"""Tests for onboarding mutations."""

from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
from opensquilla.onboarding.mutations import (
    LlmProfileActivationError,
    MutationResult,
    list_channel_entries,
    remove_channel,
    set_channel_enabled,
    upsert_audio_provider,
    upsert_channel,
    upsert_image_generation_provider,
    upsert_llm_ensemble,
    upsert_llm_provider,
    upsert_memory_embedding,
    upsert_router,
    upsert_search_provider,
    validate_channel_entry,
)
from opensquilla.onboarding.redaction import REDACTED_PLACEHOLDER
from opensquilla.router_tiers import tier_ensemble_execution


def test_upsert_provider_persists_fields():
    cfg = GatewayConfig()
    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
    )
    assert isinstance(res, MutationResult)
    assert res.config.llm.provider == "openrouter"
    assert res.config.llm.model == "deepseek/deepseek-v4-flash"
    assert res.config.llm.api_key == "sk-test"
    assert res.changed is True


def test_upsert_openrouter_can_atomically_enable_provider_default_image_generation():
    cfg = GatewayConfig()

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="openai/gpt-test",
        api_key="synthetic-openrouter-key",
        image_generation_intent="enable_provider_default",
    )

    image = res.config.image_generation
    assert image.enabled is True
    assert image.binding == "follow_llm"
    assert image.primary == "openrouter/google/gemini-3.1-flash-image-preview"
    assert image.fallbacks == []
    assert {
        "image_generation.enabled",
        "image_generation.binding",
        "image_generation.primary",
    } <= res.config.force_persist_paths()
    assert res.public_payload["capabilityChanges"]["imageGeneration"] == {
        "applied": True,
        "reason": "enabled_provider_default",
        "binding": "follow_llm",
        "primary": "openrouter/google/gemini-3.1-flash-image-preview",
    }


def test_upsert_openrouter_legacy_save_preserves_unconfigured_image_generation():
    cfg = GatewayConfig()

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="openai/gpt-test",
        api_key="synthetic-openrouter-key",
    )

    assert res.config.image_generation.enabled is False
    assert res.config.image_generation.binding == "custom"
    assert "capabilityChanges" not in res.public_payload


@pytest.mark.parametrize(
    "image_generation",
    [
        {"enabled": False},
        {
            "enabled": True,
            "primary": "openai/gpt-image-1",
            "providers": {"openai": {"api_key": "synthetic-image-key"}},
        },
    ],
)
def test_upsert_openrouter_does_not_override_operator_owned_image_generation(
    image_generation,
):
    cfg = GatewayConfig(image_generation=image_generation)

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="openai/gpt-test",
        api_key="synthetic-openrouter-key",
        image_generation_intent="enable_provider_default",
    )

    assert res.config.image_generation.model_dump() == cfg.image_generation.model_dump()
    change = res.public_payload["capabilityChanges"]["imageGeneration"]
    assert change["applied"] is False
    assert change["reason"] == "operator_configuration_preserved"


def test_upsert_openrouter_default_image_generation_rejects_custom_endpoint():
    cfg = GatewayConfig()

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="openai/gpt-test",
        api_key="synthetic-openrouter-key",
        base_url="https://openrouter-compatible.example/v1",
        image_generation_intent="enable_provider_default",
    )

    assert res.config.image_generation.enabled is False
    change = res.public_payload["capabilityChanges"]["imageGeneration"]
    assert change["applied"] is False
    assert change["reason"] == "custom_endpoint_preserved"


def test_upsert_openrouter_default_image_generation_rejects_wrong_official_path():
    cfg = GatewayConfig()

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="openai/gpt-test",
        api_key="synthetic-openrouter-key",
        base_url="https://openrouter.ai/compatible/v1",
        image_generation_intent="enable_provider_default",
    )

    assert res.config.image_generation.enabled is False
    change = res.public_payload["capabilityChanges"]["imageGeneration"]
    assert change["applied"] is False
    assert change["reason"] == "custom_endpoint_preserved"


def test_upsert_provider_rejects_unknown_image_generation_intent():
    with pytest.raises(ValueError, match="image_generation_intent"):
        upsert_llm_provider(
            GatewayConfig(),
            provider_id="openrouter",
            model="openai/gpt-test",
            api_key="synthetic-openrouter-key",
            image_generation_intent="silently_enable_everything",
        )


def test_upsert_provider_strips_trailing_paste_punctuation_from_api_key():
    cfg = GatewayConfig()
    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_key="sk-test、",
    )

    assert res.config.llm.api_key == "sk-test"


def test_provider_payload_redacts_api_key():
    cfg = GatewayConfig()
    res = upsert_llm_provider(cfg, provider_id="openrouter", model="x", api_key="sk-test")
    assert res.public_payload["api_key"] == REDACTED_PLACEHOLDER


def test_upsert_memory_embedding_local_requires_restart():
    cfg = GatewayConfig()
    res = upsert_memory_embedding(
        cfg,
        provider="local",
        model="BAAI/bge-small-zh-v1.5",
        onnx_dir="models/bge",
    )
    assert res.restart_required is True
    assert res.config.memory.embedding.requested_provider == "local"
    assert res.config.memory.embedding.local.onnx_dir == "models/bge"


def test_upsert_memory_embedding_remote_redacts_key():
    cfg = GatewayConfig()
    res = upsert_memory_embedding(
        cfg,
        provider="openai",
        model="text-embedding-3-small",
        api_key="mem-secret",
        base_url="https://api.openai.com/v1",
    )
    assert res.config.memory.embedding.remote.api_key == "mem-secret"
    assert res.public_payload["remote"]["api_key"] == REDACTED_PLACEHOLDER


def test_upsert_memory_embedding_remote_can_use_env_key_reference():
    cfg = GatewayConfig()
    res = upsert_memory_embedding(
        cfg,
        provider="openai",
        model="text-embedding-3-small",
        api_key_env="OPENAI_EMBEDDINGS_API_KEY",
        base_url="https://api.openai.com/v1",
    )

    remote = res.config.memory.embedding.remote
    assert remote.api_key in {"", None}
    assert remote.api_key_env == "OPENAI_EMBEDDINGS_API_KEY"
    assert res.public_payload["remote"]["api_key_env"] == "OPENAI_EMBEDDINGS_API_KEY"


def test_upsert_memory_embedding_auto_can_store_remote_fallback():
    cfg = GatewayConfig()
    res = upsert_memory_embedding(
        cfg,
        provider="auto",
        model="text-embedding-3-small",
        api_key="mem-secret",
        base_url="https://embeddings.example/v1",
    )
    assert res.config.memory.embedding.requested_provider == "auto"
    assert res.config.memory.embedding.remote.api_key == "mem-secret"
    assert res.config.memory.embedding.remote.base_url == "https://embeddings.example/v1"
    assert res.config.memory.embedding.remote.model == "text-embedding-3-small"
    assert res.public_payload["remote"]["api_key"] == REDACTED_PLACEHOLDER


def test_upsert_memory_embedding_explicit_remote_reuses_auto_remote_key():
    cfg = GatewayConfig(
        memory={
            "embedding": {
                "provider": "auto",
                "remote": {
                    "api_key": "mem-secret",
                    "base_url": "https://embeddings.example/v1",
                    "model": "embed-model",
                },
            }
        }
    )
    res = upsert_memory_embedding(cfg, provider="openai", api_key="")
    assert res.config.memory.embedding.requested_provider == "openai"
    assert res.config.memory.embedding.remote.api_key == "mem-secret"
    assert res.config.memory.embedding.remote.base_url == "https://embeddings.example/v1"
    assert res.config.memory.embedding.remote.model == "embed-model"


def test_upsert_memory_embedding_auto_without_changes_does_not_require_restart():
    cfg = GatewayConfig()
    res = upsert_memory_embedding(cfg, provider="auto")
    assert res.changed is False
    assert res.restart_required is False


def _remote_embedding_config(**remote: str) -> GatewayConfig:
    return GatewayConfig(
        memory={
            "embedding": {
                "provider": "openai",
                "remote": {"model": "text-embedding-3-small", **remote},
            }
        }
    )


def test_upsert_memory_embedding_remote_drops_stored_key_on_changed_origin():
    # A stored remote embedding key must not follow a changed endpoint
    # origin; the blank-key resave fails closed so the operator re-enters it.
    cfg = _remote_embedding_config(
        api_key="sk-mem-origin-a", base_url="https://a.example.test/v1"
    )

    with pytest.raises(ValueError, match="requires an api_key or api_key_env"):
        upsert_memory_embedding(
            cfg, provider="openai", api_key="", base_url="https://b.example.test/v1"
        )


def test_upsert_memory_embedding_remote_keeps_stored_key_on_same_origin():
    cfg = _remote_embedding_config(
        api_key="sk-mem-origin-a", base_url="https://a.example.test/v1"
    )

    res = upsert_memory_embedding(
        cfg, provider="openai", api_key="", base_url="https://A.example.test:443/v2"
    )

    assert res.config.memory.embedding.remote.api_key == "sk-mem-origin-a"


def test_upsert_memory_embedding_remote_env_reference_dropped_on_changed_origin():
    cfg = _remote_embedding_config(
        api_key_env="MEM_ORIGIN_A_KEY", base_url="https://a.example.test/v1"
    )

    with pytest.raises(ValueError, match="requires an api_key or api_key_env"):
        upsert_memory_embedding(
            cfg, provider="openai", base_url="https://b.example.test/v1"
        )

    same_origin = upsert_memory_embedding(
        cfg, provider="openai", base_url="https://a.example.test/v2"
    )
    assert same_origin.config.memory.embedding.remote.api_key_env == "MEM_ORIGIN_A_KEY"


def test_upsert_memory_embedding_resubmitted_env_reference_never_crosses_origin():
    # Clients hydrate and re-send the stored env-var name in plaintext: a
    # resave that re-submits the stored reference while changing the endpoint
    # origin is "keep current" and must not rebind the secret to the new
    # origin. A genuinely new env name for the new endpoint is honored.
    cfg = _remote_embedding_config(
        api_key_env="MEM_ORIGIN_A_KEY", base_url="https://a.example.test/v1"
    )

    with pytest.raises(ValueError, match="requires an api_key or api_key_env"):
        upsert_memory_embedding(
            cfg,
            provider="openai",
            api_key_env="MEM_ORIGIN_A_KEY",
            base_url="https://b.example.test/v1",
        )

    fresh_env = upsert_memory_embedding(
        cfg,
        provider="openai",
        api_key_env="MEM_ORIGIN_B_KEY",
        base_url="https://b.example.test/v1",
    )
    assert fresh_env.config.memory.embedding.remote.api_key_env == "MEM_ORIGIN_B_KEY"

    same_origin = upsert_memory_embedding(
        cfg,
        provider="openai",
        api_key_env="MEM_ORIGIN_A_KEY",
        base_url="https://a.example.test/v2",
    )
    assert same_origin.config.memory.embedding.remote.api_key_env == "MEM_ORIGIN_A_KEY"


def test_upsert_memory_embedding_auto_resubmitted_env_reference_never_crosses_origin():
    cfg = GatewayConfig(
        memory={
            "embedding": {
                "provider": "auto",
                "remote": {
                    "api_key_env": "MEM_ORIGIN_A_KEY",
                    "base_url": "https://a.example.test/v1",
                    "model": "embed-model",
                },
            }
        }
    )

    res = upsert_memory_embedding(
        cfg,
        provider="auto",
        api_key_env="MEM_ORIGIN_A_KEY",
        base_url="https://b.example.test/v1",
    )
    assert not res.config.memory.embedding.remote.api_key_env

    same_origin = upsert_memory_embedding(
        cfg,
        provider="auto",
        api_key_env="MEM_ORIGIN_A_KEY",
        base_url="https://a.example.test/v2",
    )
    assert same_origin.config.memory.embedding.remote.api_key_env == "MEM_ORIGIN_A_KEY"


def test_upsert_memory_embedding_remote_explicit_key_always_used_across_origin():
    # A freshly supplied key is operator-authored for the new endpoint and is
    # always honored, even when the origin changes.
    cfg = _remote_embedding_config(
        api_key="sk-mem-origin-a", base_url="https://a.example.test/v1"
    )

    res = upsert_memory_embedding(
        cfg,
        provider="openai",
        api_key="sk-mem-origin-b",
        base_url="https://b.example.test/v1",
    )

    assert res.config.memory.embedding.remote.api_key == "sk-mem-origin-b"


def test_unsupported_provider_rejected():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="not runtime-supported"):
        upsert_llm_provider(cfg, provider_id="github_copilot", model="x")


def test_experimental_provider_configurable_with_required_fields():
    # azure is experimental (registry-runnable, unverified) — configurable,
    # but its base_url requirement still validates.
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="base_url"):
        upsert_llm_provider(cfg, provider_id="azure", model="x", api_key="k")
    res = upsert_llm_provider(
        cfg,
        provider_id="azure",
        model="x",
        api_key="k",
        base_url="https://example.openai.azure.com/v1",
    )
    assert res.config.llm.provider == "azure"


def test_byteplus_coding_plan_provider_configurable_with_protocol_endpoint():
    cfg = GatewayConfig()
    res = upsert_llm_provider(
        cfg,
        provider_id="byteplus_coding_plan",
        model="seed-2-0-lite-260228",
        api_key="k",
    )

    assert res.changed is True
    assert res.config.llm.provider == "byteplus_coding_plan"
    assert res.config.llm.model == "seed-2-0-lite-260228"
    assert res.config.llm.api_key == "k"
    assert res.config.llm.base_url == "https://ark.ap-southeast.bytepluses.com/api/coding/v3"


def test_volcengine_coding_plan_provider_configurable():
    cfg = GatewayConfig()
    res = upsert_llm_provider(
        cfg,
        provider_id="volcengine_coding_plan",
        model="doubao-seed-2.0-pro",
        api_key="k",
    )

    assert res.changed is True
    assert res.config.llm.provider == "volcengine_coding_plan"
    assert res.config.llm.model == "doubao-seed-2.0-pro"
    assert res.config.llm.api_key == "k"
    assert res.config.llm.base_url == "https://ark.cn-beijing.volces.com/api/coding/v3"
    assert res.config.squilla_router.tiers["c0"]["model"] == "doubao-seed-2.0-lite"
    assert res.config.squilla_router.tiers["c3"]["model"] == "doubao-seed-2.0-code"


def test_ollama_does_not_require_api_key():
    cfg = GatewayConfig()
    res = upsert_llm_provider(cfg, provider_id="ollama", model="llama3.1")
    assert res.changed is True
    assert res.config.llm.provider == "ollama"


def test_provider_save_outside_packaged_presets_seeds_inline_router_tiers():
    """Provider-neutral strategy setup seeds usable tiers for every chat model.

    Non-packaged providers still persist inline tiers instead of tier_profile so
    they do not masquerade as curated presets.
    """
    cfg = GatewayConfig()
    res = upsert_llm_provider(
        cfg,
        provider_id="groq",
        model="test-model",
        api_key="sk-test",
    )
    assert res.config.llm.provider == "groq"
    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile is None
    for tier in ("c0", "c1", "c2", "c3"):
        assert res.config.squilla_router.tiers[tier]["provider"] == "groq"
        assert res.config.squilla_router.tiers[tier]["model"] == "test-model"
    persisted = res.config.to_toml_dict()["squilla_router"]
    assert "tier_profile" not in persisted
    assert persisted["tiers"]["c0"]["model"] == "test-model"


def test_tokenrhythm_provider_save_seeds_curated_inline_ladder():
    """A tokenrhythm key alone yields the curated c0-c3 ladder as inline tiers.

    The tokenrhythm preset is curated but non-persistable (its id must never
    appear as a tier_profile — downgrade contract), and the model field may be
    omitted because the preset supplies the default direct model.
    """
    cfg = GatewayConfig()
    res = upsert_llm_provider(
        cfg,
        provider_id="tokenrhythm",
        api_key="sk-test",
    )
    assert res.config.llm.provider == "tokenrhythm"
    assert res.config.llm.model == "deepseek-v4-flash-0731"
    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile is None
    expected = {
        "c0": "qwen3.7-flash",
        "c1": "deepseek-v4-flash-0731",
        "c2": "glm-5.2",
        "c3": "glm-5.2",
        "image_model": "kimi-k2.6",
    }
    for tier, model in expected.items():
        assert res.config.squilla_router.tiers[tier]["provider"] == "tokenrhythm"
        assert res.config.squilla_router.tiers[tier]["model"] == model
    persisted = res.config.to_toml_dict()["squilla_router"]
    assert "tier_profile" not in persisted
    assert persisted["tiers"]["c3"]["model"] == "glm-5.2"
    assert persisted["tiers"]["c3"]["ensemble_enabled"] is True
    assert "ensemble_selection_mode" not in persisted["tiers"]["c3"]


def test_provider_default_direct_model_does_not_follow_existing_router_tier():
    cfg = GatewayConfig(
        llm={"provider": "openrouter", "model": "", "api_key": "sk-test"},
        squilla_router={
            "default_tier": "c2",
            "tier_profile": "openrouter",
            "preset_binding": "follow_primary",
        },
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="",
    )

    assert res.config.llm.model == "deepseek/deepseek-v4-pro"
    assert res.config.squilla_router.default_tier == "c2"
    assert res.config.squilla_router.tiers["c2"]["model"] == "z-ai/glm-5.2"


def test_upsert_channel_appends_new():
    cfg = GatewayConfig()
    res = upsert_channel(
        cfg,
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "xoxb-secret",
            "signing_secret": "ss-secret",
        },
    )
    assert res.restart_required is True
    entries = list_channel_entries(res.config)
    assert len(entries) == 1
    assert entries[0]["name"] == "work"
    assert entries[0]["type"] == "slack"


def test_upsert_channel_updates_same_name():
    cfg = GatewayConfig()
    res1 = upsert_channel(
        cfg,
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "old",
            "signing_secret": "ss-old",
        },
    )
    res2 = upsert_channel(
        res1.config,
        entry_payload={"type": "slack", "name": "work", "token": "new", "slack_channel_id": "C123"},
    )
    entries = list_channel_entries(res2.config)
    assert len(entries) == 1
    assert entries[0]["slack_channel_id"] == "C123"


def test_upsert_channel_redacts_secrets_in_payload():
    cfg = GatewayConfig()
    res = upsert_channel(
        cfg,
        entry_payload={"type": "telegram", "name": "tg", "token": "abc"},
    )
    assert res.public_payload["token"] == REDACTED_PLACEHOLDER


def test_slack_webhook_channel_requires_signing_secret():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="signing_secret"):
        upsert_channel(
            cfg,
            entry_payload={"type": "slack", "name": "w", "token": "xoxb-test"},
        )


def test_slack_socket_channel_does_not_require_signing_secret():
    cfg = GatewayConfig()
    res = upsert_channel(
        cfg,
        entry_payload={
            "type": "slack",
            "name": "w",
            "token": "xoxb-test",
            "connection_mode": "socket",
            "app_token": "xapp-test",
        },
    )

    entry = list_channel_entries(res.config)[0]
    assert entry["connection_mode"] == "socket"
    assert "signing_secret" not in entry or entry["signing_secret"] in (None, "")


def test_slack_socket_channel_requires_app_token():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="app_token"):
        upsert_channel(
            cfg,
            entry_payload={
                "type": "slack",
                "name": "w",
                "token": "xoxb-test",
                "connection_mode": "socket",
            },
        )


def test_remove_channel():
    cfg = GatewayConfig()
    res1 = upsert_channel(
        cfg,
        entry_payload={"type": "slack", "name": "w", "token": "x", "signing_secret": "ss"},
    )
    res2 = remove_channel(res1.config, name="w")
    assert list_channel_entries(res2.config) == []
    assert res2.restart_required is True


def test_remove_channel_withdraws_admin_grants():
    # A dormant channel_admin_senders entry would silently re-arm for any
    # future channel created under the same name; removal must drop it while
    # leaving other channels' grants untouched.
    cfg = GatewayConfig()
    res1 = upsert_channel(
        cfg,
        entry_payload={"type": "slack", "name": "w", "token": "x", "signing_secret": "ss"},
    )
    res1.config.channel_admin_senders = {"w": ["U-1"], "other": ["Z-9"]}
    res2 = remove_channel(res1.config, name="w")
    assert list_channel_entries(res2.config) == []
    assert res2.config.channel_admin_senders == {"other": ["Z-9"]}


def test_remove_missing_channel_raises():
    cfg = GatewayConfig()
    with pytest.raises(KeyError, match="w"):
        remove_channel(cfg, name="w")


def test_set_channel_enabled_toggles():
    cfg = GatewayConfig()
    res1 = upsert_channel(
        cfg,
        entry_payload={"type": "slack", "name": "w", "token": "x", "signing_secret": "ss"},
    )
    res2 = set_channel_enabled(res1.config, name="w", enabled=False)
    assert list_channel_entries(res2.config)[0]["enabled"] is False


def test_invalid_channel_type_rejected():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="unknown channel type"):
        upsert_channel(cfg, entry_payload={"type": "nope", "name": "x"})


def test_upsert_llm_ensemble_accepts_structured_candidates_partial_merge():
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "model_options": ["legacy/model"],
            "min_successful_proposers": 2,
        }
    )

    res = upsert_llm_ensemble(
        cfg,
        candidates=[
            {
                "provider": "openrouter",
                "model": "qwen/qwen3.7-max",
                "source": "custom",
                "enabled": True,
            }
        ],
    )

    assert res.changed is True
    assert res.config.llm_ensemble.enabled is True
    assert res.config.llm_ensemble.selection_mode == "router_dynamic"
    assert res.config.llm_ensemble.model_options == ["legacy/model"]
    assert res.config.llm_ensemble.min_successful_proposers == 2
    # An omitted role canonicalizes to "proposer": the two-role contract in
    # router_tiers.ENSEMBLE_CANDIDATE_ROLES has no unassigned member, and the
    # generated web contract plus normalizeEnsembleMemberRole already coerce
    # the same way, so the public payload must not leak an empty role.
    assert [candidate.model_dump() for candidate in res.config.llm_ensemble.candidates] == [
        {
            "provider": "openrouter",
            "model": "qwen/qwen3.7-max",
            "source": "custom",
            "enabled": True,
            "role": "proposer",
            "thinking_level": "",
        }
    ]
    assert res.public_payload["candidates"] == [
        {
            "provider": "openrouter",
            "model": "qwen/qwen3.7-max",
            "source": "custom",
            "enabled": True,
            "role": "proposer",
            "thinking_level": "",
        }
    ]


def test_upsert_llm_ensemble_keeps_legacy_model_options_payload():
    cfg = GatewayConfig(llm_ensemble={"selection_mode": "router_dynamic"})

    res = upsert_llm_ensemble(cfg, model_options=[" custom/model ", "custom/model"])

    assert res.config.llm_ensemble.model_options == ["custom/model"]
    assert res.public_payload["model_options"] == ["custom/model"]


def test_telegram_webhook_requires_webhook_url():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="webhook_url"):
        upsert_channel(
            cfg,
            entry_payload={
                "type": "telegram",
                "name": "t",
                "token": "x",
                "transport_name": "webhook",
            },
        )


def test_upsert_llm_provider_preserves_existing_api_key_on_same_provider():
    cfg = GatewayConfig()
    res1 = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="m1",
        api_key="sk-existing",
        base_url="https://openrouter.ai/api/v1",
    )
    # Reconfigure model only, leaving api_key blank — should reuse existing.
    res2 = upsert_llm_provider(
        res1.config,
        provider_id="openrouter",
        model="m2",
        api_key="",
    )
    assert res2.config.llm.api_key == "sk-existing"
    assert res2.config.llm.model == "m2"


def test_upsert_llm_provider_required_key_dropped_on_changed_origin():
    # A required provider must not carry a stored explicit key to a new
    # endpoint origin: the blank-key resave drops it and fails closed so the
    # operator re-enters the key for the new endpoint.
    cfg = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "model-a",
            "api_key": "sk-required",
            "base_url": "https://a.example.test/v1",
        }
    )

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_llm_provider(
            cfg,
            provider_id="openai",
            model="model-b",
            api_key="",
            base_url="https://b.example.test/v1",
        )


def test_upsert_llm_provider_required_key_kept_on_same_origin_path_change():
    cfg = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "model-a",
            "api_key": "sk-required",
            "base_url": "https://a.example.test/v1",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="openai",
        model="model-b",
        api_key="",
        base_url="https://A.example.test:443/v2",
    )

    assert res.config.llm.api_key == "sk-required"
    assert res.config.llm.base_url == "https://A.example.test:443/v2"


def test_upsert_llm_provider_required_env_reference_dropped_on_changed_origin():
    # Env-backed required credentials follow the same boundary: a changed
    # origin drops the stored env reference and fails closed.
    cfg = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "model-a",
            "api_key_env": "OPENAI_ORIGIN_A_KEY",
            "base_url": "https://a.example.test/v1",
        }
    )

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_llm_provider(
            cfg,
            provider_id="openai",
            model="model-b",
            base_url="https://b.example.test/v1",
        )

    same_origin = upsert_llm_provider(
        cfg,
        provider_id="openai",
        model="model-b",
        base_url="https://a.example.test/v2",
    )
    assert same_origin.config.llm.api_key_env == "OPENAI_ORIGIN_A_KEY"


def test_upsert_llm_provider_resubmitted_env_reference_never_crosses_origin():
    # Clients hydrate and re-send the stored env-var name in plaintext: a
    # resave that re-submits the stored reference while changing the endpoint
    # origin is "keep current" and must not rebind the secret to the new
    # origin. A genuinely new env name for the new endpoint is honored.
    cfg = GatewayConfig(
        llm={
            "provider": "openai",
            "model": "model-a",
            "api_key_env": "OPENAI_ORIGIN_A_KEY",
            "base_url": "https://a.example.test/v1",
        }
    )

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_llm_provider(
            cfg,
            provider_id="openai",
            model="model-b",
            api_key_env="OPENAI_ORIGIN_A_KEY",
            base_url="https://b.example.test/v1",
        )

    fresh_env = upsert_llm_provider(
        cfg,
        provider_id="openai",
        model="model-b",
        api_key_env="OPENAI_ORIGIN_B_KEY",
        base_url="https://b.example.test/v1",
    )
    assert fresh_env.config.llm.api_key_env == "OPENAI_ORIGIN_B_KEY"

    same_origin = upsert_llm_provider(
        cfg,
        provider_id="openai",
        model="model-b",
        api_key_env="OPENAI_ORIGIN_A_KEY",
        base_url="https://a.example.test/v2",
    )
    assert same_origin.config.llm.api_key_env == "OPENAI_ORIGIN_A_KEY"


def test_upsert_llm_provider_preserves_optional_key_on_same_custom_provider():
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key": "sk-optional",
            "base_url": "https://llm.example.test/v1",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="custom",
        model="model-b",
        api_key="",
        preserve_api_key=True,
        base_url="https://LLM.example.test:443/v2",
    )

    assert res.config.llm.api_key == "sk-optional"
    assert res.config.llm.model == "model-b"


def test_upsert_llm_provider_optional_preserve_treats_whitespace_env_as_blank():
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key": "sk-optional",
            "base_url": "https://llm.example.test/v1",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="custom",
        model="model-b",
        api_key_env="   ",
        preserve_api_key=True,
        base_url="https://llm.example.test/v2",
    )

    assert res.config.llm.api_key == "sk-optional"
    assert res.config.llm.api_key_env == ""


@pytest.mark.parametrize(
    "candidate_base_url",
    [
        "https://other.example.test/v1",
        "http://llm.example.test/v2",
        "https://llm.example.test:444/v2",
        "https://llm.example.test:not-a-port/v2",
    ],
)
def test_upsert_llm_provider_optional_preserve_rejects_changed_or_invalid_origin(
    candidate_base_url: str,
):
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key": "sk-origin-a",
            "base_url": "https://llm.example.test/v1",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="custom",
        model="model-b",
        preserve_api_key=True,
        base_url=candidate_base_url,
    )

    assert res.config.llm.api_key == ""
    assert res.config.llm.base_url == candidate_base_url


def test_upsert_llm_provider_optional_preserve_blank_base_uses_default_origin():
    cfg = GatewayConfig(
        llm={
            "provider": "ollama",
            "model": "model-a",
            "api_key": "sk-remote-ollama",
            "base_url": "https://remote-ollama.example.test/v1",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="ollama",
        model="model-b",
        preserve_api_key=True,
        base_url="",
    )

    assert res.config.llm.base_url == "http://localhost:11434"
    assert res.config.llm.api_key == ""


def test_upsert_llm_provider_optional_preserve_omitted_base_keeps_stored_origin():
    cfg = GatewayConfig(
        llm={
            "provider": "ollama",
            "model": "model-a",
            "api_key": "sk-remote-ollama",
            "base_url": "https://remote-ollama.example.test/v1",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="ollama",
        model="model-b",
        preserve_api_key=True,
    )

    assert res.config.llm.base_url == "https://remote-ollama.example.test/v1"
    assert res.config.llm.api_key == "sk-remote-ollama"


def test_upsert_llm_provider_optional_env_reference_follows_same_origin_path():
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key_env": "CUSTOM_ORIGIN_A_KEY",
            "base_url": "https://a.example.test/v1",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="custom",
        model="model-b",
        base_url="https://A.example.test:443/alternate/v2",
    )

    assert res.config.llm.api_key_env == "CUSTOM_ORIGIN_A_KEY"


def test_upsert_llm_provider_optional_env_reference_never_crosses_origin():
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key_env": "CUSTOM_ORIGIN_A_KEY",
            "base_url": "https://a.example.test/v1",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="custom",
        model="model-b",
        base_url="https://b.example.test/v1",
    )

    assert res.config.llm.api_key_env == ""


@pytest.mark.parametrize(
    "ambient_source",
    ["stored", "registry", "settings_reference", "settings_key"],
)
def test_upsert_llm_provider_changed_origin_does_not_recover_ambient_key(
    monkeypatch,
    ambient_source: str,
):
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_ORIGIN_A_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_SETTINGS_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY_ENV", raising=False)
    if ambient_source == "stored":
        monkeypatch.setenv("CUSTOM_ORIGIN_A_KEY", "sk-ambient-origin-a")
    elif ambient_source == "registry":
        monkeypatch.setenv("CUSTOM_LLM_API_KEY", "sk-ambient-origin-a")
    elif ambient_source == "settings_reference":
        monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY_ENV", "CUSTOM_SETTINGS_KEY")
        monkeypatch.setenv("CUSTOM_SETTINGS_KEY", "sk-ambient-origin-a")
    else:
        monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "sk-ambient-origin-a")
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key_env": "CUSTOM_ORIGIN_A_KEY",
            "base_url": "https://a.example.test/v1",
        }
    )

    with pytest.raises(ValueError, match="ambient credential is active"):
        upsert_llm_provider(
            cfg,
            provider_id="custom",
            model="model-b",
            base_url="https://b.example.test/v1",
        )


def test_upsert_llm_provider_changed_origin_accepts_new_env_reference(monkeypatch):
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "sk-ambient-origin-a")
    monkeypatch.setenv("CUSTOM_ORIGIN_B_KEY", "sk-explicit-origin-b")
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key_env": "CUSTOM_ORIGIN_A_KEY",
            "base_url": "https://a.example.test/v1",
        }
    )

    changed = upsert_llm_provider(
        cfg,
        provider_id="custom",
        model="model-b",
        api_key_env="CUSTOM_ORIGIN_B_KEY",
        base_url="https://b.example.test/v1",
    )
    runtime = resolve_llm_runtime_config(changed.config)

    assert changed.config.llm.api_key_env == "CUSTOM_ORIGIN_B_KEY"
    assert runtime.api_key == "sk-explicit-origin-b"
    assert runtime.api_key_env_name == "CUSTOM_ORIGIN_B_KEY"


def test_upsert_llm_provider_changed_origin_unset_new_env_blocks_generic_fallback(
    monkeypatch,
):
    monkeypatch.delenv("CUSTOM_ORIGIN_B_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "sk-generic-origin-a")
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key": "",
            "api_key_env": "CUSTOM_ORIGIN_A_KEY",
            "base_url": "https://a.example.test/v1",
        }
    )

    changed = upsert_llm_provider(
        cfg,
        provider_id="custom",
        model="model-b",
        api_key_env="CUSTOM_ORIGIN_B_KEY",
        base_url="https://b.example.test/v1",
    )
    runtime = resolve_llm_runtime_config(changed.config)

    assert changed.config.llm.api_key_env == "CUSTOM_ORIGIN_B_KEY"
    assert runtime.api_key == ""
    assert runtime.api_key_from_env is False
    assert runtime.api_key_env_name == "CUSTOM_ORIGIN_B_KEY"


def test_upsert_llm_provider_clears_optional_key_by_default():
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key": "sk-optional",
            "base_url": "https://llm.example.test/v1",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="custom",
        model="model-b",
        api_key="",
        base_url="https://llm.example.test/v1",
    )

    assert res.config.llm.api_key == ""


def test_upsert_llm_provider_optional_preserve_yields_to_env_replacement():
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key": "sk-optional",
            "base_url": "https://llm.example.test/v1",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="custom",
        model="model-b",
        api_key_env="CUSTOM_REPLACEMENT_KEY",
        preserve_api_key=True,
        base_url="https://llm.example.test/v1",
    )

    assert res.config.llm.api_key == ""
    assert res.config.llm.api_key_env == "CUSTOM_REPLACEMENT_KEY"


def test_upsert_llm_provider_optional_preserve_never_crosses_providers():
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key": "sk-custom",
            "base_url": "https://llm.example.test/v1",
        },
        squilla_router={"enabled": False},
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="ollama",
        model="llama3.1",
        preserve_api_key=True,
    )

    assert res.config.llm.provider == "ollama"
    assert res.config.llm.api_key == ""


def test_upsert_llm_provider_optional_preserve_ignores_oauth_stale_key():
    cfg = GatewayConfig(
        llm={
            "provider": "openai_codex",
            "model": "gpt-5.1-codex",
            "api_key": "stale-key",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="openai_codex",
        model="gpt-5.1-codex",
        preserve_api_key=True,
    )

    assert res.config.llm.api_key == ""


def test_upsert_llm_provider_optional_preserve_requires_stored_explicit_key():
    cfg = GatewayConfig(
        llm={
            "provider": "custom",
            "model": "model-a",
            "api_key": "runtime-env-value",
            "base_url": "https://llm.example.test/v1",
        }
    )
    cfg.mark_runtime_secret("llm.api_key")

    res = upsert_llm_provider(
        cfg,
        provider_id="custom",
        model="model-b",
        preserve_api_key=True,
        base_url="https://llm.example.test/v1",
    )

    assert res.config.llm.api_key == ""


def test_upsert_llm_provider_can_use_env_key_without_secret():
    cfg = GatewayConfig()
    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_key="",
        api_key_env="OPENROUTER_API_KEY",
    )

    assert res.config.llm.api_key == ""
    assert res.config.llm.api_key_env == "OPENROUTER_API_KEY"
    assert res.public_payload["api_key_source"] == "env"


def test_upsert_llm_provider_recomputes_router_preset_on_provider_switch():
    cfg = GatewayConfig(
        llm={"provider": "openrouter", "model": "deepseek/x"},
        squilla_router={"preset_binding": "follow_primary"},
    )
    assert cfg.squilla_router.enabled is True
    # Fresh OpenRouter configs remain the openrouter-mix/direct-router shape.
    # Saving a different packaged provider below should still recompute that
    # provider's compact router profile.
    assert cfg.squilla_router.tier_profile is None

    res = upsert_llm_provider(
        cfg,
        provider_id="deepseek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
    )

    assert res.config.llm.provider == "deepseek"
    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile == "deepseek"
    assert res.config.squilla_router.tiers["c0"]["provider"] == "deepseek"
    assert "tiers" not in res.config.to_toml_dict()["squilla_router"]


def test_upsert_llm_provider_without_preset_follows_managed_router_binding():
    cfg = GatewayConfig(
        llm={"provider": "openrouter", "model": "deepseek/x"},
        squilla_router={"preset_binding": "follow_primary"},
    )

    res = upsert_llm_provider(cfg, provider_id="deepseek", model="deepseek-chat",
                              api_key_env="DEEPSEEK_API_KEY")

    persisted = res.config.to_toml_dict()["squilla_router"]
    assert persisted["tier_profile"] == "deepseek"
    assert "tiers" not in persisted


def test_first_provider_save_establishes_follow_primary_binding():
    cfg = GatewayConfig()

    res = upsert_llm_provider(
        cfg,
        provider_id="deepseek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
    )

    assert res.config.squilla_router.preset_binding == "follow_primary"
    assert res.config.squilla_router.tier_profile == "deepseek"
    assert res.config.to_toml_dict()["squilla_router"]["preset_binding"] == (
        "follow_primary"
    )


def test_first_provider_save_recognizes_materialized_pristine_defaults():
    pristine = GatewayConfig()
    # Runtime resolution and config.get -> config.apply both materialize the
    # otherwise implicit defaults. Rebuilding from the public-shaped payload
    # reproduces that loss of pydantic field-presence provenance without any
    # network or credential dependency.
    cfg = GatewayConfig(
        llm=pristine.llm.model_dump(mode="python"),
        squilla_router=pristine.squilla_router.model_dump(mode="python"),
    )
    assert "provider" in cfg.llm.model_fields_set
    assert cfg.squilla_router.model_fields_set

    res = upsert_llm_provider(
        cfg,
        provider_id="deepseek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
    )

    assert res.config.llm.provider == "deepseek"
    assert res.config.llm.model == "deepseek-chat"
    assert res.config.squilla_router.preset_binding == "follow_primary"
    assert res.config.squilla_router.tier_profile == "deepseek"


def test_materialized_default_with_explicit_custom_binding_still_conflicts():
    pristine = GatewayConfig()
    router_payload = pristine.squilla_router.model_dump(mode="python")
    router_payload["preset_binding"] = "custom"
    cfg = GatewayConfig(
        llm=pristine.llm.model_dump(mode="python"),
        squilla_router=router_payload,
    )

    with pytest.raises(LlmProfileActivationError) as error:
        upsert_llm_provider(
            cfg,
            provider_id="deepseek",
            model="deepseek-chat",
            api_key_env="DEEPSEEK_API_KEY",
        )

    assert error.value.reason == "router_provider_conflict"
    assert error.value.details["conflictProviders"] == ["tokenrhythm"]


def test_managed_provider_switch_preserves_router_controls_and_ensemble():
    cfg = GatewayConfig(
        llm={"provider": "openrouter", "model": "deepseek/x"},
        squilla_router={
            "enabled": False,
            "preset_binding": "follow_primary",
            "default_tier": "c2",
            "visual_mode": "legacy_grid",
            "cross_provider_tiers": True,
            "tier_provider_mismatch": "veto",
            "confidence_threshold": 0.73,
        },
        llm_ensemble={
            "enabled": False,
            "candidate_max_chars": 12_345,
            "record_candidates": True,
        },
    )
    ensemble_before = cfg.llm_ensemble.model_dump(mode="python")

    res = upsert_llm_provider(
        cfg,
        provider_id="deepseek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
    )

    router = res.config.squilla_router
    assert router.enabled is False
    assert router.preset_binding == "follow_primary"
    assert router.tier_profile is None
    assert router.default_tier == "c2"
    assert router.visual_mode == "legacy_grid"
    assert router.cross_provider_tiers is True
    assert router.tier_provider_mismatch == "veto"
    assert router.confidence_threshold == 0.73
    assert {router.tiers[name]["provider"] for name in ("c0", "c1", "c2", "c3")} == {
        "deepseek"
    }
    assert res.config.llm_ensemble.model_dump(mode="python") == ensemble_before


def test_same_provider_save_does_not_reject_existing_custom_foreign_tier():
    cfg = GatewayConfig(
        llm={"provider": "openai", "model": "gpt-test", "api_key": "old-key"},
        squilla_router={
            "preset_binding": "custom",
            "cross_provider_tiers": False,
        },
    )
    cfg.squilla_router.tiers["c0"] = {
        "provider": "deepseek",
        "model": "deepseek-chat",
    }
    router_before = cfg.squilla_router.model_dump(mode="python")

    res = upsert_llm_provider(
        cfg,
        provider_id="openai",
        model="gpt-test",
        api_key="new-key",
    )

    assert res.config.squilla_router.model_dump(mode="python") == router_before


def test_upsert_llm_provider_preset_id_legacy_writes_recommended_shape():
    # Explicit legacy-nine presetId == today's recommended path: enabled,
    # persisted tier_profile, no inline tiers.
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "deepseek/x"})

    res = upsert_llm_provider(
        cfg,
        provider_id="deepseek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
        preset_id="deepseek",
    )

    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile == "deepseek"
    persisted = res.config.to_toml_dict()["squilla_router"]
    assert persisted["tier_profile"] == "deepseek"
    assert "tiers" not in persisted


def test_upsert_llm_provider_preset_id_applies_default_model_when_model_omitted():
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "deepseek/x"})

    res = upsert_llm_provider(
        cfg,
        provider_id="deepseek",
        preset_id="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
    )

    # deepseek preset default_model is deepseek-v4-flash.
    assert res.config.llm.model == "deepseek-v4-flash"


def test_upsert_llm_provider_preset_id_synthesized_writes_custom_shape():
    # A synthesized preset id (== provider id) writes the custom-mode shape:
    # enabled, tier_profile=None, expanded tiers, with empty model slots filled
    # by the effective model.
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "deepseek/x"})

    res = upsert_llm_provider(
        cfg,
        provider_id="groq",
        model="llama-3.3-70b",
        api_key_env="GROQ_API_KEY",
        preset_id="groq",
    )

    router = res.config.squilla_router
    assert router.enabled is True
    assert router.tier_profile is None
    for tier in ("c0", "c1", "c2", "c3"):
        assert router.tiers[tier]["provider"] == "groq"
        assert router.tiers[tier]["model"] == "llama-3.3-70b"
    persisted = res.config.to_toml_dict()["squilla_router"]
    assert "tier_profile" not in persisted
    assert persisted["tiers"]["c0"]["model"] == "llama-3.3-70b"


def test_upsert_llm_provider_curated_synthesized_preset_preserves_ladder():
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "deepseek/x"})

    res = upsert_llm_provider(
        cfg,
        provider_id="qianfan",
        api_key_env="QIANFAN_API_KEY",
        preset_id="qianfan",
    )

    router = res.config.squilla_router
    assert router.enabled is True
    assert router.tier_profile is None
    assert res.config.llm.model == "ernie-4.5-turbo-128k"
    assert router.tiers["c0"]["model"] == "ernie-4.5-turbo-128k"
    assert router.tiers["c3"]["model"] == "ernie-4.5-turbo-128k"
    assert router.tiers["image_model"]["model"] == "ernie-4.5-turbo-vl-32k"
    assert router.tiers["image_model"]["image_only"] is True
    persisted = res.config.to_toml_dict()["squilla_router"]
    assert "tier_profile" not in persisted
    assert persisted["tiers"]["c0"]["model"] == "ernie-4.5-turbo-128k"


def test_upsert_llm_provider_preset_id_must_match_provider():
    cfg = GatewayConfig()

    with pytest.raises(ValueError, match="does not apply to provider"):
        upsert_llm_provider(
            cfg,
            provider_id="deepseek",
            model="deepseek-chat",
            api_key_env="DEEPSEEK_API_KEY",
            preset_id="openai",
        )


def test_upsert_router_recommended_writes_profile_without_expanded_tiers():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(cfg, mode="recommended")

    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile == "deepseek"
    assert "tiers" not in res.config.to_toml_dict()["squilla_router"]
    assert res.public_payload["mode"] == "recommended"


def test_upsert_router_materializes_the_shared_tokenrhythm_plan_without_global_enable():
    cfg = GatewayConfig()

    res = upsert_router(cfg, mode="recommended")

    assert res.config.squilla_router.tiers["c3"]["ensemble_enabled"] is True
    assert res.config.llm_ensemble.enabled is False
    assert res.config.llm_ensemble.selection_mode == "static_tokenrhythm_b5"
    assert res.config.llm_ensemble.min_successful_proposers == 1
    assert res.config.llm_ensemble.proposer_max_retries == 1
    assert res.config.llm_ensemble.all_failed_policy == "fallback_single"
    assert "llm_ensemble.selection_mode" in res.config.force_persist_paths()
    assert "llm_ensemble.min_successful_proposers" in res.config.force_persist_paths()
    assert "llm_ensemble.proposer_max_retries" in res.config.force_persist_paths()
    assert "llm_ensemble.all_failed_policy" in res.config.force_persist_paths()


def test_upsert_router_preserves_explicit_shared_ensemble_policy() -> None:
    cfg = GatewayConfig(
        llm_ensemble={
            "min_successful_proposers": 4,
            "proposer_max_retries": 2,
            "all_failed_policy": "error",
        }
    )

    res = upsert_router(cfg, mode="recommended")

    assert res.config.squilla_router.tiers["c3"]["ensemble_enabled"] is True
    assert res.config.llm_ensemble.min_successful_proposers == 4
    assert res.config.llm_ensemble.proposer_max_retries == 2
    assert res.config.llm_ensemble.all_failed_policy == "error"


@pytest.mark.parametrize("tier", ["c0", "c1", "c2"])
def test_upsert_router_rejects_shared_ensemble_flag_outside_c3(tier: str):
    cfg = GatewayConfig()

    with pytest.raises(
        ValueError,
        match=rf"router tier {tier!r} ensembleEnabled is only supported for c3",
    ):
        upsert_router(
            cfg,
            mode="custom",
            tiers={tier: {"ensembleEnabled": True}},
        )


def test_upsert_router_disabled_does_not_materialize_a_dormant_shared_tier():
    cfg = GatewayConfig()

    res = upsert_router(cfg, mode="disabled")

    assert res.config.squilla_router.enabled is False
    assert res.config.llm_ensemble.selection_mode == "static_openrouter_b5"
    assert "llm_ensemble.selection_mode" not in res.config.force_persist_paths()


def test_upsert_router_legacy_mode_does_not_inherit_the_shared_preset_flag():
    cfg = GatewayConfig(
        llm_ensemble={
            "enabled": False,
            "selection_mode": "static_openrouter_b5",
        }
    )

    res = upsert_router(
        cfg,
        mode="custom",
        tiers={
            "c3": {
                "provider": "tokenrhythm",
                "model": "glm-5.2",
                "ensembleSelectionMode": "static_tokenrhythm_b5",
            }
        },
    )

    c3 = res.config.squilla_router.tiers["c3"]
    assert "ensemble_enabled" not in c3
    assert c3["ensemble_selection_mode"] == "static_tokenrhythm_b5"
    assert tier_ensemble_execution(
        res.config.squilla_router.tiers,
        "c3",
        shared_selection_mode=res.config.llm_ensemble.selection_mode,
    ) == ("static_tokenrhythm_b5", "legacy")


def test_upsert_router_persists_explicit_single_model_over_the_recommended_c3_default():
    cfg = GatewayConfig()

    res = upsert_router(
        cfg,
        mode="custom",
        tiers={
            "c3": {
                "provider": "tokenrhythm",
                "model": "glm-5.2",
                "ensembleEnabled": False,
                "ensembleSelectionMode": "",
            }
        },
    )

    c3 = res.config.squilla_router.tiers["c3"]
    assert c3["ensemble_enabled"] is False
    assert c3["ensemble_selection_mode"] == ""
    persisted = res.config.to_toml_dict()["squilla_router"]["tiers"]["c3"]
    assert persisted["ensemble_enabled"] is False
    assert res.public_payload["mode"] == "custom"


def test_upsert_router_forces_image_model_role_invariants():
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "z-ai/glm-5.1"})

    res = upsert_router(
        cfg,
        mode="openrouter-mix",
        tiers={
            "image_model": {
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "supportsImage": False,
                "image_only": False,
            }
        },
    )

    image_tier = res.config.squilla_router.tiers["image_model"]
    assert image_tier["model"] == "anthropic/claude-opus-4.8"
    assert image_tier["supports_image"] is True
    assert image_tier["image_only"] is True


def test_upsert_router_can_disable():
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "deepseek/x"})

    res = upsert_router(cfg, mode="disabled")

    assert res.config.squilla_router.enabled is False
    assert res.config.squilla_router.tier_profile is None
    assert res.public_payload["mode"] == "disabled"


def test_upsert_router_rejects_openrouter_mix_for_direct_provider():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    with pytest.raises(ValueError, match="openrouter-mix"):
        upsert_router(cfg, mode="openrouter-mix")


def test_upsert_router_custom_is_accepted_for_any_provider():
    # custom is the provider-agnostic generalization of openrouter-mix: for a
    # legacy-nine provider it seeds tiers from the packaged preset but writes
    # NO tier_profile.
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(cfg, mode="custom")

    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile is None
    assert res.config.squilla_router.tiers["c0"]["provider"] == "deepseek"
    assert res.config.squilla_router.tiers["c0"]["model"] == "deepseek-v4-flash"
    assert res.public_payload["mode"] == "custom"
    assert res.public_payload["tier_profile"] is None
    # With no persisted profile the effective tiers persist expanded inline.
    persisted = res.config.to_toml_dict()["squilla_router"]
    assert "tier_profile" not in persisted
    assert persisted["tiers"]["c2"]["model"] == "deepseek-v4-pro"


def test_upsert_router_custom_merges_provided_tiers_over_preset():
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "z-ai/glm-5.1"})

    res = upsert_router(
        cfg,
        mode="custom",
        tiers={"c3": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"}},
    )

    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile is None
    assert res.config.squilla_router.tiers["c3"]["model"] == "anthropic/claude-opus-4.8"
    # Unoverridden tiers keep the openrouter preset values.
    assert res.config.squilla_router.tiers["c0"]["provider"] == "openrouter"


def test_upsert_router_rejects_unknown_tier_ensemble_selection_mode():
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "z-ai/glm-5.1"})

    with pytest.raises(ValueError, match="ensembleSelectionMode must be one of"):
        upsert_router(
            cfg,
            mode="custom",
            tiers={
                "c3": {
                    "provider": "openrouter",
                    "model": "z-ai/glm-5.2",
                    "ensembleSelectionMode": "static_openrouter_typo",
                }
            },
        )


def test_upsert_router_can_enable_cross_provider_tiers():
    cfg = GatewayConfig(llm={"provider": "openai", "model": "gpt-5.4-mini"})

    res = upsert_router(
        cfg,
        mode="custom",
        cross_provider_tiers=True,
        tier_provider_mismatch="veto",
        tiers={
            "c0": {"provider": "openai", "model": "gpt-5.4-mini"},
            "c1": {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            "c2": {"provider": "openrouter", "model": "z-ai/glm-5.2"},
            "c3": {"provider": "openai", "model": "gpt-5.5"},
        },
    )

    router = res.config.squilla_router
    assert router.enabled is True
    assert router.tier_profile is None
    assert router.cross_provider_tiers is True
    assert router.tier_provider_mismatch == "veto"
    assert router.tiers["c1"]["provider"] == "openrouter"
    assert res.public_payload["cross_provider_tiers"] is True
    assert res.public_payload["tier_provider_mismatch"] == "veto"


def test_upsert_router_rejects_unknown_tier_provider_mismatch_policy():
    cfg = GatewayConfig(llm={"provider": "openai", "model": "gpt-5.4-mini"})

    with pytest.raises(ValueError, match="tierProviderMismatch"):
        upsert_router(
            cfg,
            mode="custom",
            tier_provider_mismatch="drop",
            tiers={
                "c0": {"provider": "openai", "model": "gpt-5.4-mini"},
                "c1": {"provider": "openai", "model": "gpt-5.4"},
                "c2": {"provider": "openai", "model": "gpt-5.5-mini"},
                "c3": {"provider": "openai", "model": "gpt-5.5"},
            },
        )


def test_upsert_router_custom_seeds_current_model_for_synthesized_presets():
    # Non-packaged providers have no curated ladder, so custom mode seeds every
    # tier from the current Chat Model instead of leaving empty routes.
    cfg = GatewayConfig(llm={"provider": "groq", "model": "m"})

    res = upsert_router(cfg, mode="custom")

    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile is None
    for tier in ("c0", "c1", "c2", "c3"):
        assert res.config.squilla_router.tiers[tier]["provider"] == "groq"
        assert res.config.squilla_router.tiers[tier]["model"] == "m"
    assert res.public_payload["mode"] == "custom"


def test_upsert_router_custom_accepts_explicit_tiers_for_synthesized_presets():
    cfg = GatewayConfig(llm={"provider": "groq", "model": "m"})

    res = upsert_router(
        cfg,
        mode="custom",
        tiers={
            "c0": {"model": "groq-fast"},
            "c1": {"model": "groq-mid"},
            "c2": {"model": "groq-strong"},
            "c3": {"model": "groq-max"},
        },
    )

    assert res.config.squilla_router.enabled is True
    assert res.config.squilla_router.tier_profile is None
    assert res.config.squilla_router.tiers["c1"] == {
        "provider": "groq",
        "model": "groq-mid",
        "description": (
            "groq balanced route (synthesized default; no curated per-tier model ladder)."
        ),
        "supports_image": False,
    }
    # Router tier selection is independent from the direct/fallback model.
    assert res.config.llm.model == "m"


def test_upsert_router_custom_preserves_direct_fallback_model():
    cfg = GatewayConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})

    res = upsert_router(cfg, mode="custom", default_tier="c2")

    assert res.config.squilla_router.default_tier == "c2"
    assert res.config.llm.model == "deepseek-chat"


def test_upsert_router_mixed_default_does_not_copy_foreign_model_to_primary():
    cfg = GatewayConfig(llm={"provider": "openai", "model": "gpt-4o-mini"})

    res = upsert_router(
        cfg,
        mode="custom",
        default_tier="c0",
        cross_provider_tiers=True,
        tier_provider_mismatch="veto",
        tiers={
            "c0": {"provider": "deepseek", "model": "deepseek-chat"},
            "c1": {"provider": "openai", "model": "gpt-4o-mini"},
            "c2": {"provider": "openai", "model": "gpt-4o-mini"},
            "c3": {"provider": "openai", "model": "gpt-4o-mini"},
        },
    )

    assert res.config.squilla_router.tiers["c0"]["model"] == "deepseek-chat"
    assert res.config.llm.provider == "openai"
    assert res.config.llm.model == "gpt-4o-mini"


def test_upsert_llm_provider_keeps_runtime_secret_marker_when_reusing_key():
    cfg = GatewayConfig()
    cfg.llm.provider = "openrouter"
    cfg.llm.model = "m1"
    cfg.llm.api_key = "from-env"
    cfg.mark_runtime_secret("llm.api_key")

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="m2",
        api_key="",
    )

    assert res.config.llm.api_key == "from-env"
    assert "llm.api_key" in res.config._runtime_secret_paths


def test_upsert_llm_provider_clears_runtime_secret_marker_for_explicit_key():
    cfg = GatewayConfig()
    cfg.llm.provider = "openrouter"
    cfg.llm.model = "m1"
    cfg.llm.api_key = "from-env"
    cfg.mark_runtime_secret("llm.api_key")

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="m2",
        api_key="sk-written",
    )

    assert res.config.llm.api_key == "sk-written"
    assert "llm.api_key" not in res.config._runtime_secret_paths


def test_upsert_llm_provider_explicit_key_clears_existing_env_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "m1",
            "api_key": "",
            "api_key_env": "OPENROUTER_API_KEY",
        }
    )

    res = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="m2",
        api_key="sk-written",
    )
    runtime = resolve_llm_runtime_config(res.config)

    assert res.config.llm.api_key_env == ""
    assert runtime.api_key == "sk-written"
    assert runtime.api_key_from_env is False


def test_upsert_llm_provider_rejects_ambiguous_key_sources():
    cfg = GatewayConfig()

    with pytest.raises(ValueError, match="either api_key or api_key_env"):
        upsert_llm_provider(
            cfg,
            provider_id="openrouter",
            model="m",
            api_key="sk-written",
            api_key_env="OPENROUTER_API_KEY",
        )


def test_upsert_llm_provider_does_not_carry_key_across_providers():
    cfg = GatewayConfig()
    res1 = upsert_llm_provider(
        cfg,
        provider_id="openrouter",
        model="m",
        api_key="sk-or",
    )
    # Switching to a different key-required provider must require a new key.
    with pytest.raises(ValueError, match="api_key"):
        upsert_llm_provider(
            res1.config,
            provider_id="openai",
            model="gpt-4o",
            api_key="",
        )


def test_validate_channel_entry_returns_normalized_payload():
    out = validate_channel_entry(
        {"type": "slack", "name": "w", "token": "x", "signing_secret": "ss"}
    )
    assert out["type"] == "slack"
    assert out["enabled"] is True
    assert out["agent_id"] == "main"


def test_upsert_search_provider_configures_brave():
    cfg = GatewayConfig()
    res = upsert_search_provider(
        cfg,
        provider_id="brave",
        api_key="brave-key",
        max_results=7,
        proxy="http://127.0.0.1:7890",
        use_env_proxy=True,
        fallback_policy="network",
        diagnostics=True,
    )
    assert res.config.search_provider == "brave"
    assert res.config.search_api_key == "brave-key"
    assert res.config.search_max_results == 7
    assert res.config.search_proxy == "http://127.0.0.1:7890"
    assert res.config.search_use_env_proxy is True
    assert res.config.search_fallback_policy == "network"
    assert res.config.search_diagnostics is True
    assert res.public_payload["api_key"] == REDACTED_PLACEHOLDER


def test_upsert_search_provider_configures_tavily_with_api_key():
    cfg = GatewayConfig()
    res = upsert_search_provider(
        cfg,
        provider_id="tavily",
        api_key="tavily-key",
    )

    assert res.config.search_provider == "tavily"
    assert res.config.search_api_key == "tavily-key"
    assert res.config.search_api_key_env == ""
    assert res.public_payload["api_key"] == REDACTED_PLACEHOLDER
    assert res.public_payload["api_key_source"] == "explicit"


def test_upsert_search_provider_strips_trailing_paste_punctuation_from_api_key():
    cfg = GatewayConfig()
    res = upsert_search_provider(cfg, provider_id="brave", api_key="brave-key、")

    assert res.config.search_api_key == "brave-key"


def test_upsert_search_provider_can_use_env_key_reference():
    cfg = GatewayConfig()
    res = upsert_search_provider(
        cfg,
        provider_id="brave",
        api_key="",
        api_key_env="BRAVE_SEARCH_API_KEY",
    )
    assert res.config.search_provider == "brave"
    assert res.config.search_api_key == ""
    assert res.config.search_api_key_env == "BRAVE_SEARCH_API_KEY"
    assert res.public_payload["api_key_source"] == "env"


def test_upsert_search_provider_can_use_tavily_env_key_reference():
    cfg = GatewayConfig()
    res = upsert_search_provider(
        cfg,
        provider_id="tavily",
        api_key="",
        api_key_env="TAVILY_API_KEY",
    )

    assert res.config.search_provider == "tavily"
    assert res.config.search_api_key == ""
    assert res.config.search_api_key_env == "TAVILY_API_KEY"
    assert res.public_payload["api_key_source"] == "env"


def test_upsert_search_provider_accepts_webui_string_max_results():
    cfg = GatewayConfig()
    res = upsert_search_provider(
        cfg,
        provider_id="duckduckgo",
        max_results="5",
    )

    assert res.config.search_provider == "duckduckgo"
    assert res.config.search_max_results == 5


def test_upsert_search_provider_clears_env_key_for_no_key_provider():
    cfg = GatewayConfig(search_provider="brave", search_api_key_env="BRAVE_SEARCH_API_KEY")

    res = upsert_search_provider(
        cfg,
        provider_id="duckduckgo",
        api_key_env="BRAVE_SEARCH_API_KEY",
    )

    assert res.config.search_provider == "duckduckgo"
    assert res.config.search_api_key_env == ""
    assert res.public_payload["api_key_source"] == "none"


def test_upsert_image_generation_provider_configures_openrouter(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig()
    res = upsert_image_generation_provider(
        cfg,
        provider_id="openrouter",
        primary="openrouter/google/gemini-3.1-flash-image-preview",
        api_key="sk-or",
    )
    assert res.config.image_generation.enabled is True
    assert res.config.image_generation.primary == "openrouter/google/gemini-3.1-flash-image-preview"
    assert res.config.image_generation.providers.openrouter.api_key == "sk-or"
    assert res.public_payload["api_key"] == REDACTED_PLACEHOLDER
    assert res.public_payload["api_key_source"] == "explicit"


def test_upsert_image_generation_redacted_key_keeps_direct_credential_with_env_echo(
    monkeypatch,
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    first = upsert_image_generation_provider(
        GatewayConfig(),
        provider_id="openrouter",
        api_key="sk-stored-direct-key",
    )

    res = upsert_image_generation_provider(
        first.config,
        provider_id="openrouter",
        api_key="***",
        api_key_env="OPENROUTER_API_KEY",
    )

    provider = res.config.image_generation.providers.openrouter
    assert provider.api_key == "sk-stored-direct-key"
    assert res.public_payload["api_key_source"] == "explicit"


def test_upsert_image_generation_legacy_direct_key_accepts_default_env_echo(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    res = upsert_image_generation_provider(
        GatewayConfig(),
        provider_id="openrouter",
        primary=_IMG_PRIMARY,
        api_key="sk-new-direct-key",
        api_key_env="OPENROUTER_API_KEY",
    )

    provider = res.config.image_generation.providers.openrouter
    assert provider.api_key == "sk-new-direct-key"
    assert provider.api_key_env == ""
    assert res.public_payload["api_key_source"] == "explicit"


def test_upsert_image_generation_legacy_default_env_echo_keeps_stored_direct_key(
    monkeypatch,
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig(
        image_generation={
            "enabled": True,
            "primary": _IMG_PRIMARY,
            "providers": {
                "openrouter": {
                    "api_key": "sk-stored-direct-key",
                    "api_key_env": "OPENROUTER_API_KEY",
                }
            },
        }
    )

    res = upsert_image_generation_provider(
        cfg,
        provider_id="openrouter",
        primary=_IMG_PRIMARY,
        api_key_env="OPENROUTER_API_KEY",
    )

    provider = res.config.image_generation.providers.openrouter
    assert provider.api_key == "sk-stored-direct-key"
    assert res.public_payload["api_key_source"] == "explicit"


def test_upsert_image_generation_legacy_direct_key_rejects_custom_env_echo():
    with pytest.raises(ValueError, match="either api_key or api_key_env"):
        upsert_image_generation_provider(
            GatewayConfig(),
            provider_id="openrouter",
            primary=_IMG_PRIMARY,
            api_key="sk-new-direct-key",
            api_key_env="CUSTOM_IMAGE_KEY",
        )


def test_upsert_image_generation_provider_can_use_matching_llm_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig()
    cfg.llm.provider = "openrouter"
    cfg.llm.api_key = "sk-llm"
    res = upsert_image_generation_provider(cfg, provider_id="openrouter")
    assert res.config.image_generation.enabled is True
    assert res.config.image_generation.providers.openrouter.api_key == ""
    assert res.public_payload["api_key_source"] == "llm_fallback"


def test_upsert_image_generation_reuses_profile_without_copying_its_key(monkeypatch):
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "openrouter/auto",
            "api_key": "synthetic-primary-key",
            "base_url": "https://openrouter.ai/api/v1",
        },
        llm_profiles={
            "tokenrhythm": {
                "model": "deepseek-v4-flash",
                "api_key": "synthetic-profile-key",
                "base_url": "https://tokenrhythm.studio/v1",
            }
        },
    )

    result = upsert_image_generation_provider(cfg, provider_id="tokenrhythm")

    assert result.public_payload["api_key_source"] == "llm_fallback"
    assert result.config.image_generation.providers.tokenrhythm.api_key == ""
    assert result.config.image_generation.providers.tokenrhythm.api_key_env == ""


def test_upsert_image_generation_provider_llm_fallback_requires_same_origin(monkeypatch):
    # The matching-LLM-key fallback binds the primary LLM secret to the image
    # endpoint; a save pointing the image provider at a different origin must
    # not silently bind it and instead requires a dedicated key.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = GatewayConfig()
    cfg.llm.provider = "openai"
    cfg.llm.api_key = "sk-real"
    cfg.llm.base_url = "https://api.openai.com/v1"

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_image_generation_provider(
            cfg,
            provider_id="openai",
            base_url="https://other.example.com/v1",
            api_key="",
            enabled=True,
        )

    same_origin = upsert_image_generation_provider(
        cfg,
        provider_id="openai",
        base_url="https://api.openai.com/images/path",
        api_key="",
    )
    assert same_origin.public_payload["api_key_source"] == "llm_fallback"


def test_upsert_image_generation_provider_can_disable_without_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig()

    res = upsert_image_generation_provider(
        cfg,
        provider_id="openrouter",
        primary="openrouter/google/gemini-3.1-flash-image-preview",
        enabled=False,
    )

    assert res.config.image_generation.enabled is False
    assert res.config.image_generation.primary == "openrouter/google/gemini-3.1-flash-image-preview"
    assert res.public_payload["api_key_source"] == "none"


def test_upsert_image_generation_provider_rejects_wrong_primary_provider():
    cfg = GatewayConfig()
    cfg.llm.provider = "openrouter"
    cfg.llm.api_key = "sk-llm"
    with pytest.raises(ValueError, match="provider/model"):
        upsert_image_generation_provider(
            cfg,
            provider_id="openrouter",
            primary="openai/gpt-image-1",
        )


def test_upsert_image_generation_provider_drops_stored_key_on_changed_origin(
    monkeypatch,
):
    # A stored explicit image key must not follow a changed endpoint origin.
    # The disabled resave lets us observe the cleared key directly.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    first = upsert_image_generation_provider(
        GatewayConfig(),
        provider_id="openai",
        api_key="sk-image-origin-a",
        base_url="https://a.example.test/v1",
    )

    changed = upsert_image_generation_provider(
        first.config,
        provider_id="openai",
        api_key="",
        base_url="https://b.example.test/v1",
        enabled=False,
    )

    provider_cfg = changed.config.image_generation.providers.openai
    assert provider_cfg.api_key == ""
    assert provider_cfg.base_url == "https://b.example.test/v1"


def test_upsert_image_generation_provider_changed_origin_fails_closed(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    first = upsert_image_generation_provider(
        GatewayConfig(),
        provider_id="openai",
        api_key="sk-image-origin-a",
        base_url="https://a.example.test/v1",
    )

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_image_generation_provider(
            first.config,
            provider_id="openai",
            api_key="",
            base_url="https://b.example.test/v1",
        )


def test_upsert_image_generation_provider_keeps_stored_key_on_same_origin(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    first = upsert_image_generation_provider(
        GatewayConfig(),
        provider_id="openai",
        api_key="sk-image-origin-a",
        base_url="https://a.example.test/v1",
    )

    same = upsert_image_generation_provider(
        first.config,
        provider_id="openai",
        api_key="",
        base_url="https://A.example.test:443/v2",
    )

    assert same.config.image_generation.providers.openai.api_key == "sk-image-origin-a"


def test_upsert_image_generation_default_env_key_never_crosses_origin(monkeypatch):
    # The well-known default env var resolves the same shared real secret,
    # so a cross-origin resave must not keep resolving it for the new
    # endpoint: the enabled resave fails closed and the disabled resave
    # persists no env reference.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared-image-env")
    first = upsert_image_generation_provider(
        GatewayConfig(),
        provider_id="openai",
        api_key="sk-image-origin-a",
        base_url="https://api.openai.com/v1",
    )

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_image_generation_provider(
            first.config,
            provider_id="openai",
            api_key="***",
            base_url="https://attacker.example/v1",
        )

    changed = upsert_image_generation_provider(
        first.config,
        provider_id="openai",
        api_key="***",
        base_url="https://attacker.example/v1",
        enabled=False,
    )
    provider_cfg = changed.config.image_generation.providers.openai
    assert provider_cfg.api_key == ""
    assert provider_cfg.api_key_env != "OPENAI_API_KEY"
    assert changed.public_payload["api_key_source"] != "env"


def test_upsert_image_generation_does_not_restore_default_env_after_origin_change(
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared-image-env")
    first = upsert_image_generation_provider(GatewayConfig(), provider_id="openai")
    changed = upsert_image_generation_provider(
        first.config,
        provider_id="openai",
        api_key="***",
        base_url="https://other.example.test/v1",
        enabled=False,
    )

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_image_generation_provider(
            changed.config,
            provider_id="openai",
            base_url="https://other.example.test/v2",
            enabled=True,
        )

    assert changed.config.image_generation.providers.openai.api_key_env == ""
    reauthorized = upsert_image_generation_provider(
        changed.config,
        provider_id="openai",
        api_key_env="OPENAI_API_KEY",
        base_url="https://other.example.test/v2",
        enabled=True,
    )
    assert reauthorized.config.image_generation.providers.openai.api_key_env == (
        "OPENAI_API_KEY"
    )
    assert (
        "image_generation",
        "providers",
        "openai",
        "api_key_env",
    ) in reauthorized.config.force_persist_path_segments()


def test_upsert_image_generation_uses_default_env_without_persisting_schema_default(
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared-image-env")
    res = upsert_image_generation_provider(GatewayConfig(), provider_id="openai")
    assert res.config.image_generation.providers.openai.api_key_env == ""
    assert res.public_payload["api_key_source"] == "env"
    assert res.public_payload["api_key_source"] == "env"


def test_upsert_image_generation_same_origin_path_keeps_implicit_default_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared-image-env")
    first = upsert_image_generation_provider(GatewayConfig(), provider_id="openai")

    same = upsert_image_generation_provider(
        first.config,
        provider_id="openai",
        base_url="https://api.openai.com/v2",
    )

    assert same.config.image_generation.providers.openai.api_key_env == ""
    assert same.public_payload["api_key_source"] == "env"
    assert same.public_payload["api_key_source"] == "env"


def test_upsert_image_generation_resubmitted_env_reference_never_crosses_origin(monkeypatch):
    # Clients hydrate and re-send the stored env-var name in plaintext: a
    # resave that re-submits the stored reference while changing the endpoint
    # origin is "keep current" and must not rebind the secret to the new
    # origin.
    monkeypatch.setenv("MY_IMG_KEY", "sk-image-env-origin-a")
    first = upsert_image_generation_provider(
        GatewayConfig(),
        provider_id="openai",
        api_key_env="MY_IMG_KEY",
        base_url="https://a.example.test/v1",
    )
    assert first.config.image_generation.providers.openai.api_key_env == "MY_IMG_KEY"

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_image_generation_provider(
            first.config,
            provider_id="openai",
            api_key_env="MY_IMG_KEY",
            base_url="https://b.example.test/v1",
        )

    changed = upsert_image_generation_provider(
        first.config,
        provider_id="openai",
        api_key_env="MY_IMG_KEY",
        base_url="https://b.example.test/v1",
        enabled=False,
    )
    assert changed.config.image_generation.providers.openai.api_key_env == ""

    same_origin = upsert_image_generation_provider(
        first.config,
        provider_id="openai",
        api_key_env="MY_IMG_KEY",
        base_url="https://a.example.test/v2",
    )
    assert same_origin.config.image_generation.providers.openai.api_key_env == "MY_IMG_KEY"


def test_upsert_audio_provider_drops_stored_key_on_changed_origin(monkeypatch):
    # A stored explicit audio key must not follow a changed endpoint origin;
    # with every credential source dropped the enabled resave fails closed.
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    first = upsert_audio_provider(
        GatewayConfig(),
        provider_id="elevenlabs",
        api_key="sk-audio-origin-a",
        base_url="https://a.example.test/v1",
    )

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_audio_provider(
            first.config,
            provider_id="elevenlabs",
            api_key="",
            base_url="https://b.example.test/v1",
        )

    changed = upsert_audio_provider(
        first.config,
        provider_id="elevenlabs",
        api_key="",
        base_url="https://b.example.test/v1",
        enabled=False,
    )

    provider_cfg = changed.config.audio.providers.elevenlabs
    assert provider_cfg.api_key == ""
    assert provider_cfg.api_key_env == ""
    assert provider_cfg.base_url == "https://b.example.test/v1"


def test_upsert_audio_provider_default_env_key_never_crosses_origin(monkeypatch):
    # The well-known default env var resolves the same shared real secret,
    # so a cross-origin resave must not keep resolving it for the new
    # endpoint.
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-shared-audio-env")
    first = upsert_audio_provider(
        GatewayConfig(),
        provider_id="elevenlabs",
        api_key="sk-audio-origin-a",
        base_url="https://a.example.test/v1",
    )

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_audio_provider(
            first.config,
            provider_id="elevenlabs",
            api_key="***",
            base_url="https://b.example.test/v1",
        )

    changed = upsert_audio_provider(
        first.config,
        provider_id="elevenlabs",
        api_key="***",
        base_url="https://b.example.test/v1",
        enabled=False,
    )
    provider_cfg = changed.config.audio.providers.elevenlabs
    assert provider_cfg.api_key == ""
    assert provider_cfg.api_key_env != "ELEVENLABS_API_KEY"
    assert changed.public_payload["api_key_source"] != "env"


def test_upsert_audio_provider_does_not_restore_default_env_after_origin_change(
    monkeypatch,
):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-shared-audio-env")
    first = upsert_audio_provider(GatewayConfig(), provider_id="elevenlabs")
    changed = upsert_audio_provider(
        first.config,
        provider_id="elevenlabs",
        api_key="***",
        base_url="https://other.example.test/v1",
        enabled=False,
    )

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_audio_provider(
            changed.config,
            provider_id="elevenlabs",
            base_url="https://other.example.test/v2",
            enabled=True,
        )

    assert changed.config.audio.providers.elevenlabs.api_key_env == ""
    reauthorized = upsert_audio_provider(
        changed.config,
        provider_id="elevenlabs",
        api_key_env="ELEVENLABS_API_KEY",
        base_url="https://other.example.test/v2",
        enabled=True,
    )
    assert reauthorized.config.audio.providers.elevenlabs.api_key_env == (
        "ELEVENLABS_API_KEY"
    )
    assert (
        "audio",
        "providers",
        "elevenlabs",
        "api_key_env",
    ) in reauthorized.config.force_persist_path_segments()


def test_upsert_audio_provider_resubmitted_env_reference_never_crosses_origin(monkeypatch):
    # Clients hydrate and re-send the stored env-var name in plaintext: a
    # resave that re-submits the stored reference while changing the endpoint
    # origin is "keep current" and must not rebind the secret to the new
    # origin.
    monkeypatch.setenv("MY_AUDIO_KEY", "sk-audio-env-origin-a")
    first = upsert_audio_provider(
        GatewayConfig(),
        provider_id="elevenlabs",
        api_key_env="MY_AUDIO_KEY",
        base_url="https://a.example.test/v1",
    )
    assert first.config.audio.providers.elevenlabs.api_key_env == "MY_AUDIO_KEY"

    with pytest.raises(ValueError, match="requires an api_key"):
        upsert_audio_provider(
            first.config,
            provider_id="elevenlabs",
            api_key_env="MY_AUDIO_KEY",
            base_url="https://b.example.test/v1",
        )

    changed = upsert_audio_provider(
        first.config,
        provider_id="elevenlabs",
        api_key_env="MY_AUDIO_KEY",
        base_url="https://b.example.test/v1",
        enabled=False,
    )
    assert changed.config.audio.providers.elevenlabs.api_key_env == ""

    same_origin = upsert_audio_provider(
        first.config,
        provider_id="elevenlabs",
        api_key_env="MY_AUDIO_KEY",
        base_url="https://a.example.test/v2",
    )
    assert same_origin.config.audio.providers.elevenlabs.api_key_env == "MY_AUDIO_KEY"


def test_upsert_audio_provider_keeps_stored_key_on_same_origin(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    first = upsert_audio_provider(
        GatewayConfig(),
        provider_id="elevenlabs",
        api_key="sk-audio-origin-a",
        base_url="https://a.example.test/v1",
    )

    same = upsert_audio_provider(
        first.config,
        provider_id="elevenlabs",
        api_key="",
        base_url="https://A.example.test:443/v2",
    )

    assert same.config.audio.providers.elevenlabs.api_key == "sk-audio-origin-a"


def test_search_provider_requiring_key_can_reuse_existing_key():
    cfg = GatewayConfig(search_provider="brave", search_api_key="old")
    res = upsert_search_provider(cfg, provider_id="brave", api_key="")
    assert res.config.search_api_key == "old"


def test_search_provider_requiring_key_rejects_missing_key():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="api_key"):
        upsert_search_provider(cfg, provider_id="brave", api_key="", api_key_env="")


def test_unsupported_search_provider_rejected():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="not runtime-supported"):
        upsert_search_provider(cfg, provider_id="perplexity", api_key="k")


def test_upsert_channel_preserves_secret_when_blank():
    cfg = GatewayConfig()
    first = upsert_channel(
        cfg,
        entry_payload={
            "type": "slack",
            "name": "w",
            "token": "xoxb-original",
            "signing_secret": "ss-original",
        },
    )
    second = upsert_channel(
        first.config,
        entry_payload={
            "type": "slack",
            "name": "w",
            "token": "",  # blank = keep current
            "signing_secret": "",
            "slack_channel_id": "C999",
        },
    )
    raw = [e.model_dump(mode="python") for e in second.config.channels.channels]
    entry = next(e for e in raw if e["name"] == "w")
    assert entry["token"] == "xoxb-original"
    assert entry["signing_secret"] == "ss-original"
    assert entry["slack_channel_id"] == "C999"


def test_upsert_channel_replaces_secret_when_provided():
    cfg = GatewayConfig()
    first = upsert_channel(
        cfg,
        entry_payload={
            "type": "slack",
            "name": "w",
            "token": "xoxb-old",
            "signing_secret": "ss-old",
        },
    )
    second = upsert_channel(
        first.config,
        entry_payload={"type": "slack", "name": "w", "token": "xoxb-new"},
    )
    raw = [e.model_dump(mode="python") for e in second.config.channels.channels]
    entry = next(e for e in raw if e["name"] == "w")
    assert entry["token"] == "xoxb-new"


_IMG_PRIMARY = "openrouter/google/gemini-3.1-flash-image-preview"


def test_upsert_image_generation_applies_size_format_fallbacks():
    cfg = GatewayConfig()
    res = upsert_image_generation_provider(
        cfg,
        provider_id="openrouter",
        primary=_IMG_PRIMARY,
        api_key="sk-img",
        size="1536x1024",
        output_format="webp",
        fallbacks=["openai/gpt-image-1", "  openrouter/x  ", ""],
    )
    ig = res.config.image_generation
    assert ig.size == "1536x1024"
    assert ig.output_format == "webp"
    assert ig.fallbacks == ["openai/gpt-image-1", "openrouter/x"]
    assert res.public_payload["size"] == "1536x1024"
    assert res.public_payload["fallbacks"] == ["openai/gpt-image-1", "openrouter/x"]


def test_upsert_image_generation_canonicalizes_openrouter_auto_model():
    res = upsert_image_generation_provider(
        GatewayConfig(),
        provider_id="openrouter",
        primary="openrouter/auto",
        api_key="sk-img",
        fallbacks=["openrouter/auto"],
    )

    assert res.config.image_generation.primary == "openrouter/openrouter/auto"
    assert res.config.image_generation.fallbacks == ["openrouter/openrouter/auto"]


def test_upsert_image_generation_legacy_switch_normalization_requires_saved_source_fields():
    cfg = GatewayConfig(
        image_generation={
            "enabled": True,
            "primary": "openai/custom-image-model",
            "fallbacks": ["openai/gpt-image-1"],
            "providers": {
                "openai": {"base_url": "https://images.example.test/v1"},
            },
        }
    )

    with pytest.raises(ValueError, match="primary must be a provider/model reference"):
        upsert_image_generation_provider(
            cfg,
            provider_id="openrouter",
            primary="openai/custom-image-model",
            api_key="sk-img",
            api_key_env="OPENROUTER_API_KEY",
            # This differs from the stored source endpoint, so it is not an
            # old-client draft and must not receive compatibility rewriting.
            base_url="https://untrusted.example.test/v1",
            fallbacks=["openai/gpt-image-1"],
        )


def test_upsert_image_generation_empty_keeps_current():
    cfg = GatewayConfig()
    cfg.image_generation.size = "1024x1536"
    cfg.image_generation.output_format = "jpeg"
    cfg.image_generation.fallbacks = ["openrouter/keep"]
    res = upsert_image_generation_provider(
        cfg, provider_id="openrouter", primary=_IMG_PRIMARY, api_key="sk-img"
    )
    ig = res.config.image_generation
    assert ig.size == "1024x1536"
    assert ig.output_format == "jpeg"
    assert ig.fallbacks == ["openrouter/keep"]


def test_upsert_image_generation_legacy_empty_fallbacks_keep_current():
    cfg = GatewayConfig()
    cfg.image_generation.fallbacks = ["openrouter/keep"]

    res = upsert_image_generation_provider(
        cfg,
        provider_id="openrouter",
        primary=_IMG_PRIMARY,
        api_key="sk-img",
        fallbacks=[],
    )

    assert res.config.image_generation.fallbacks == ["openrouter/keep"]


def test_upsert_image_generation_explicit_empty_fallbacks_clear_current():
    cfg = GatewayConfig()
    cfg.image_generation.fallbacks = ["openrouter/keep"]

    res = upsert_image_generation_provider(
        cfg,
        provider_id="openrouter",
        primary=_IMG_PRIMARY,
        api_key="sk-img",
        fallbacks=[],
        clear_fallbacks=True,
    )

    assert res.config.image_generation.fallbacks == []
    assert res.public_payload["fallbacks"] == []


def test_upsert_image_generation_allows_authenticated_fallback_on_primary_metadata_save(
    monkeypatch,
):
    from opensquilla.onboarding.section_status import (
        SectionStatus,
        image_generation_section_status,
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig(
        image_generation={
            "enabled": True,
            "primary": _IMG_PRIMARY,
            "fallbacks": ["openai/gpt-image-1"],
            "providers": {"openai": {"api_key": "sk-fallback"}},
        }
    )

    res = upsert_image_generation_provider(
        cfg,
        provider_id="openrouter",
        primary=_IMG_PRIMARY,
        output_format="webp",
    )

    assert res.config.image_generation.output_format == "webp"
    assert res.config.image_generation.fallbacks == ["openai/gpt-image-1"]
    assert image_generation_section_status(res.config) is SectionStatus.OK


def test_upsert_image_generation_env_reference_replaces_stored_direct_key():
    first = upsert_image_generation_provider(
        GatewayConfig(),
        provider_id="openrouter",
        primary=_IMG_PRIMARY,
        api_key="sk-image-direct",
    )

    replaced = upsert_image_generation_provider(
        first.config,
        provider_id="openrouter",
        primary=_IMG_PRIMARY,
        api_key_env="OPENSQUILLA_TEST_IMAGE_KEY",
        credential_mode="env",
    )

    provider = replaced.config.image_generation.providers.openrouter
    assert provider.api_key == ""
    assert provider.api_key_env == "OPENSQUILLA_TEST_IMAGE_KEY"


@pytest.mark.parametrize(
    ("primary", "fallbacks"),
    [
        ("openrouter/", None),
        ("openrouter/google//image", None),
        ("openrouter/google image", None),
        (_IMG_PRIMARY, ["openai/"]),
        (_IMG_PRIMARY, ["openai/gpt image"]),
    ],
)
def test_upsert_image_generation_rejects_malformed_model_references(primary, fallbacks):
    with pytest.raises(ValueError, match="provider/model|Invalid image generation model ref"):
        upsert_image_generation_provider(
            GatewayConfig(),
            provider_id="openrouter",
            primary=primary,
            api_key="sk-img",
            fallbacks=fallbacks,
        )


def test_upsert_image_generation_rejects_foreign_official_endpoint():
    with pytest.raises(ValueError, match="cannot use the official 'openai' endpoint"):
        upsert_image_generation_provider(
            GatewayConfig(),
            provider_id="openrouter",
            primary=_IMG_PRIMARY,
            api_key="sk-img",
            base_url="https://api.openai.com/v1",
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "not-a-url",
        "https://openrouter.ai:invalid/v1",
        "https://openrouter.ai:99999/v1",
        "https://openrouter.ai/api/v1?tenant=test",
        "https://openrouter.ai/api/v1#fragment",
        "https://user:secret@openrouter.ai/api/v1",
    ],
)
def test_upsert_image_generation_rejects_invalid_endpoint(base_url):
    with pytest.raises(ValueError, match="absolute http:// or https:// URL"):
        upsert_image_generation_provider(
            GatewayConfig(),
            provider_id="openrouter",
            primary=_IMG_PRIMARY,
            api_key="sk-img",
            base_url=base_url,
        )


def test_upsert_image_generation_can_disable_unchanged_legacy_invalid_config():
    cfg = GatewayConfig(
        image_generation={
            "enabled": True,
            "primary": "openrouter/google//image",
            "fallbacks": ["openai/"],
            "providers": {
                "openrouter": {
                    "api_key": "sk-synthetic-image",
                    "base_url": "not-a-url",
                }
            },
        }
    )

    res = upsert_image_generation_provider(
        cfg,
        provider_id="openrouter",
        primary=cfg.image_generation.primary,
        fallbacks=list(cfg.image_generation.fallbacks),
        enabled=False,
    )

    assert res.config.image_generation.enabled is False
    assert res.config.image_generation.primary == "openrouter/google//image"
    assert res.config.image_generation.fallbacks == ["openai/"]
    provider = res.config.image_generation.providers.openrouter
    assert provider.base_url == "not-a-url"
    assert provider.api_key == "sk-synthetic-image"


def test_upsert_image_generation_rejects_bad_size():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="image size"):
        upsert_image_generation_provider(
            cfg, provider_id="openrouter", primary=_IMG_PRIMARY, api_key="sk-img", size="999x999"
        )


def test_upsert_image_generation_rejects_bad_fallback_reference():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="fallback"):
        upsert_image_generation_provider(
            cfg, provider_id="openrouter", primary=_IMG_PRIMARY, api_key="sk-img",
            fallbacks=["no-slash-ref"],
        )


def test_validate_channel_entry_error_never_echoes_secret_values():
    secret = "tg-super-secret-token-value-1234567890"

    with pytest.raises(ValueError, match="webhook_url") as exc_info:
        validate_channel_entry(
            {
                "type": "telegram",
                "name": "t",
                "transport_name": "webhook",
                "token": secret,
            }
        )

    message = str(exc_info.value)
    assert secret not in message
    assert secret[-12:] not in message
    assert "input_value" not in message


def test_upsert_channel_error_never_echoes_secret_values():
    cfg = GatewayConfig()
    secret = "tg-super-secret-token-value-1234567890"

    with pytest.raises(ValueError, match="webhook_url") as exc_info:
        upsert_channel(
            cfg,
            entry_payload={
                "type": "telegram",
                "name": "t",
                "transport_name": "webhook",
                "token": secret,
            },
        )

    message = str(exc_info.value)
    assert secret not in message
    assert secret[-12:] not in message


def test_upsert_channel_rejects_blank_required_secret_for_new_entry():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="token"):
        upsert_channel(cfg, entry_payload={"type": "telegram", "name": "t", "token": ""})


def test_upsert_channel_rejects_whitespace_only_required_secret_for_new_entry():
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="token"):
        upsert_channel(cfg, entry_payload={"type": "telegram", "name": "t", "token": "   "})


def test_validate_channel_entry_rejects_blank_required_secret():
    with pytest.raises(ValueError, match="token"):
        validate_channel_entry({"type": "telegram", "name": "t", "token": ""})


def test_validate_channel_entry_blank_secret_skips_inapplicable_show_when_fields():
    # slack socket mode: signing_secret is required only for webhook mode,
    # so the blank-secret gate must not fire for it here.
    out = validate_channel_entry(
        {
            "type": "slack",
            "name": "w",
            "token": "xoxb-test",
            "connection_mode": "socket",
            "app_token": "xapp-test",
        }
    )
    assert out["connection_mode"] == "socket"


def test_upsert_channel_whitespace_only_secret_keeps_existing_value():
    cfg = GatewayConfig()
    first = upsert_channel(
        cfg,
        entry_payload={"type": "telegram", "name": "t", "token": "tg-original"},
    )
    second = upsert_channel(
        first.config,
        entry_payload={"type": "telegram", "name": "t", "token": "   "},
    )
    raw = [e.model_dump(mode="python") for e in second.config.channels.channels]
    entry = next(e for e in raw if e["name"] == "t")
    assert entry["token"] == "tg-original"


def test_router_reconcile_survives_out_of_band_llm_model_change():
    """R6: a machine-seeded ladder must stay recognizable after llm.model
    changed out-of-band (config.set RPC / TOML hand-edit): a re-save naming
    the new model explicitly must reseed the tiers instead of leaving every
    routed turn pinned to the old model."""
    cfg = GatewayConfig()
    seeded = upsert_llm_provider(
        cfg,
        provider_id="anthropic",
        model="model-alpha",
        api_key="sk-old",
    ).config
    for tier in ("c0", "c1", "c2", "c3"):
        assert seeded.squilla_router.tiers[tier]["model"] == "model-alpha"

    # Out-of-band hot-apply: llm.model changes without a provider save.
    seeded.llm.model = "model-beta"

    rotated = upsert_llm_provider(
        seeded,
        provider_id="anthropic",
        model="model-beta",
        api_key="sk-new",
    ).config
    assert rotated.llm.model == "model-beta"
    for tier in ("c0", "c1", "c2", "c3"):
        assert rotated.squilla_router.tiers[tier]["model"] == "model-beta"


def test_router_reconcile_still_preserves_hand_authored_ladder():
    """The R6 widening must not regress the hand-customized guard: a ladder
    that matches no machine seeding survives a same-provider re-save."""
    cfg = GatewayConfig()
    seeded = upsert_llm_provider(
        cfg,
        provider_id="anthropic",
        model="model-alpha",
        api_key="sk-old",
    ).config
    router_payload = seeded.squilla_router.model_dump(mode="python")
    router_payload["preset_binding"] = "custom"
    router_payload["tiers"]["c0"]["model"] = "model-cheap"
    router_payload["tiers"]["c3"]["model"] = "model-big"
    from opensquilla.gateway.config import SquillaRouterConfig

    seeded.squilla_router = SquillaRouterConfig(**router_payload)

    rotated = upsert_llm_provider(
        seeded,
        provider_id="anthropic",
        model="model-alpha",
        api_key="sk-new",
    ).config
    assert rotated.squilla_router.tiers["c0"]["model"] == "model-cheap"
    assert rotated.squilla_router.tiers["c3"]["model"] == "model-big"


def test_upsert_router_non_registry_provider_gets_actionable_error():
    """X1: a hand-edited non-registry llm.provider used to die on the cryptic
    "router tier 'c0' must be an object"; the error must now name the
    provider and point at the two runnable ways out."""
    cfg = GatewayConfig()
    cfg.llm.provider = "acme-llm"
    cfg.llm.model = "acme-model"

    with pytest.raises(ValueError) as exc_info:
        upsert_router(cfg, mode="recommended", default_tier="c1")

    message = str(exc_info.value)
    assert "acme-llm" in message
    assert "opensquilla onboard configure provider --provider" in message
    assert "--router disabled" in message
    assert "must be an object" not in message


def test_upsert_router_non_registry_provider_disabled_still_works():
    cfg = GatewayConfig()
    cfg.llm.provider = "acme-llm"
    cfg.llm.model = "acme-model"
    res = upsert_router(cfg, mode="disabled")
    assert res.config.squilla_router.enabled is False


def test_image_generation_explicit_enabled_decision_is_force_persisted(tmp_path):
    """R1: an explicit enabled=false on a fresh config equals the model
    default, so the sparse diff alone would drop it — and a later key
    rotation would re-enable image generation via configure-implies-enable.
    The mutation must force the decision into the file."""
    import tomllib as _tomllib

    from opensquilla.onboarding.config_store import load_config, persist_config

    target = tmp_path / "config.toml"
    cfg = load_config(target)
    res = upsert_image_generation_provider(
        cfg,
        provider_id="openai",
        primary="openai/gpt-image-1",
        api_key="sk-image-1",
        enabled=False,
    )
    persist_config(res.config, path=target)

    data = _tomllib.loads(target.read_text())
    assert data["image_generation"]["enabled"] is False


def test_audio_explicit_enabled_decision_is_force_persisted(tmp_path):
    """An old client may still explicitly disable audio while saving a key."""
    import tomllib as _tomllib

    from opensquilla.onboarding.config_store import load_config, persist_config

    target = tmp_path / "config.toml"
    cfg = load_config(target)
    res = upsert_audio_provider(
        cfg,
        provider_id="elevenlabs",
        api_key="synthetic-audio-key",
        enabled=False,
    )
    persist_config(res.config, path=target)

    data = _tomllib.loads(target.read_text())
    assert data["audio"]["enabled"] is False


def test_upsert_channel_blank_secret_keeps_stored_value():
    cfg = GatewayConfig()
    res1 = upsert_channel(
        cfg,
        entry_payload={"type": "telegram", "name": "tg", "token": "tok-stored"},
    )
    for blank in ("", "   ", None):
        payload: dict[str, object] = {"type": "telegram", "name": "tg", "default_chat_id": "42"}
        if blank is not None:
            payload["token"] = blank
        res = upsert_channel(res1.config, entry_payload=payload)
        raw = res.config.channels.channels[0]
        assert raw.token == "tok-stored"
        assert raw.default_chat_id == "42"


def test_upsert_channel_redaction_placeholder_keeps_stored_value():
    # '***' is what channels.get / probe echo for a stored secret; a client
    # round-tripping that payload means "keep the current value". Enforced
    # server-side so every RPC/CLI client gets the same trust boundary (the
    # Web UI scrub is defense in depth only).
    cfg = GatewayConfig()
    res1 = upsert_channel(
        cfg,
        entry_payload={"type": "telegram", "name": "tg", "token": "tok-real"},
    )
    res2 = upsert_channel(
        res1.config,
        entry_payload={"type": "telegram", "name": "tg", "token": REDACTED_PLACEHOLDER},
    )
    # Raw config access: the redacted *listing* would mask a real value too.
    assert res2.config.channels.channels[0].token == "tok-real"


def test_upsert_channel_redaction_placeholder_without_stored_value_rejected():
    # With no stored credential to keep, persisting the literal sentinel
    # would overwrite the channel's token with asterisks — reject instead.
    cfg = GatewayConfig()
    with pytest.raises(ValueError, match="looks redacted"):
        upsert_channel(
            cfg,
            entry_payload={
                "type": "telegram",
                "name": "tg",
                "token": REDACTED_PLACEHOLDER,
            },
        )


def test_upsert_channel_same_name_different_type_replaces_without_secret_merge():
    # The replace loop matches on name only, while the keep-current secret
    # merge matches on name AND type: re-saving an existing name under a new
    # type replaces the entry and preserves no secrets. The Web UI duplicate
    # guard words its warning ("Save will replace it") on this behavior.
    cfg = GatewayConfig()
    res1 = upsert_channel(
        cfg,
        entry_payload={"type": "telegram", "name": "bridge", "token": "tg-secret"},
    )
    with pytest.raises(ValueError):
        # Blank secret cannot borrow the other type's stored value.
        upsert_channel(res1.config, entry_payload={"type": "discord", "name": "bridge"})
    res2 = upsert_channel(
        res1.config,
        entry_payload={"type": "discord", "name": "bridge", "token": "dc-secret"},
    )
    raw_entries = res2.config.channels.channels
    assert len(raw_entries) == 1
    assert raw_entries[0].type == "discord"
    assert raw_entries[0].token == "dc-secret"


def test_validate_channel_entry_raises_structured_field_errors():
    from opensquilla.onboarding.mutations import ChannelValidationError, validate_channel_entry

    with pytest.raises(ChannelValidationError) as ei:
        validate_channel_entry({"type": "slack"})  # missing required name/token
    assert ei.value.field_errors, "expected per-field detail"
    assert all("field" in fe and "message" in fe for fe in ei.value.field_errors)
    # The joined string message is preserved for callers that only read str(exc).
    assert "invalid channel entry" in str(ei.value)
