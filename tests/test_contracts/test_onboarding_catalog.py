"""Wire-contract freeze for the ``onboarding.catalog`` RPC payload.

The catalog is consumed by the Web UI onboarding wizard and by external
control clients, so its key names are a public protocol contract (see
CLAUDE.md: public RPC field names are stable). These tests pin today's
exact key sets:

- Renaming or removing any frozen key is a contract break and must fail here.
- Adding a key is allowed, but for the exact-key-set assertions below it
  requires deliberately updating the frozen set in this file — that friction
  is the point: additions to the wire surface should be a conscious decision.

Everything asserted here is built from pure in-process catalog builders
(``provider_catalog_payload`` / ``router_catalog_payload`` and the
``onboarding.catalog`` handler); no network, credentials, or gateway boot.
"""

from __future__ import annotations

from opensquilla.gateway import rpc_onboarding
from opensquilla.gateway.config import ROUTER_TIER_PROFILE_IDS
from opensquilla.gateway.rpc import RpcContext
from opensquilla.onboarding.provider_specs import provider_catalog_payload
from opensquilla.onboarding.router_specs import router_catalog_payload

# Top-level catalog sections the Web UI switches on. Exact set: a renamed or
# dropped section silently blanks a wizard page.
CATALOG_TOP_LEVEL_KEYS = frozenset(
    {
        "providers",
        "channels",
        "searchProviders",
        "routerProfiles",
        "memoryEmbeddingProviders",
        "imageGenerationProviders",
        "audioProviders",
    }
)

# Per-provider entry shape rendered by the provider picker.
PROVIDER_ENTRY_KEYS = frozenset(
    {
        "providerId",
        "label",
        "backend",
        "providerKind",
        "runtimeSupported",
        "verification",
        "envKey",
        "defaultBaseUrl",
        # Deliberate additive credential-capability metadata: unlike
        # requiresApiKey, this distinguishes optional API-key support from
        # OAuth providers that do not accept an API key at all.
        "acceptsApiKey",
        "requiresApiKey",
        "requiresBaseUrl",
        "routerSupported",
        "deployment",
        "blocking",
        "canProbe",
        "readmeScenarios",
        "whatYouNeed",
        "defaultDirectModel",
        # Deliberate preset-surface additions: each provider entry now carries
        # its registry preset(s) and the preset-declared default model. Adding
        # these to the frozen set is the conscious decision the friction forces.
        "defaultModel",
        "presets",
        "capabilities",
        "fields",
    }
)

# Per-preset entry shape rendered by the preset picker (exactly one preset per
# provider today; the wire is a list to allow future multi-preset providers).
PROVIDER_PRESET_KEYS = frozenset(
    {"presetId", "label", "description", "synthesized", "defaultModel", "tiers"}
)

# Per-field spec shape the wizard uses to render setup form inputs.
PROVIDER_FIELD_KEYS = frozenset(
    {
        "name",
        "label",
        "type",
        "required",
        "default",
        "description",
        "secret",
    }
)

# Router catalog shapes: profile list + per-tier route cards.
ROUTER_CATALOG_TOP_LEVEL_KEYS = frozenset({"defaultTier", "textTiers", "modes", "profiles"})
ROUTER_MODE_KEYS = frozenset({"mode", "label", "description"})
ROUTER_PROFILE_KEYS = frozenset({"profileId", "providerId", "label", "tiers"})
ROUTER_TIER_PAYLOAD_KEYS = frozenset(
    {"provider", "model", "description", "thinkingLevel", "supportsImage"}
)

# The canonical text tiers (c0-c3, per router_tiers.py) and the nine tier
# profiles shipped today. Hardcoded on purpose: dropping a profile or renaming
# a tier changes what onboarding clients can select, so it must fail here even
# if the source constant is edited in the same change.
FROZEN_TEXT_TIERS = ("c0", "c1", "c2", "c3")
FROZEN_ROUTER_PROFILE_IDS = frozenset(
    {
        "byteplus",
        "dashscope",
        "deepseek",
        "gemini",
        "moonshot",
        "openai",
        "openrouter",
        "volcengine",
        "zhipu",
    }
)
# Curated presets that are NOT persistable tier_profile ids: packaged tier
# data applied as inline tiers (synthesized=False on the wire, but the id
# stays outside FROZEN_ROUTER_PROFILE_IDS).
FROZEN_CURATED_INLINE_PRESET_IDS = frozenset({"tokenrhythm"})


async def test_onboarding_catalog_top_level_sections_are_frozen() -> None:
    # The handler ignores params and only reads pure catalog builders, so a
    # bare RpcContext is enough — no gateway boot required.
    payload = await rpc_onboarding._onboarding_catalog(None, RpcContext(conn_id="contract"))
    assert set(payload) == CATALOG_TOP_LEVEL_KEYS


def test_provider_catalog_entry_keys_are_frozen() -> None:
    entries = provider_catalog_payload()
    assert entries, "provider catalog must not be empty"
    for entry in entries:
        assert set(entry) == PROVIDER_ENTRY_KEYS, entry["providerId"]
        assert entry["fields"], entry["providerId"]
        for field in entry["fields"]:
            assert set(field) == PROVIDER_FIELD_KEYS, (entry["providerId"], field.get("name"))

    # The default provider must stay addressable by its shipped id.
    provider_ids = {entry["providerId"] for entry in entries}
    assert "openrouter" in provider_ids


def test_provider_catalog_preset_keys_are_frozen() -> None:
    entries = provider_catalog_payload()
    for entry in entries:
        presets = entry["presets"]
        # Exactly one registry preset per runtime-supported provider today —
        # widening to several curated presets is an allowed (deliberate)
        # evolution, but silently losing the preset is a break.
        assert len(presets) == 1, entry["providerId"]
        for preset in presets:
            assert set(preset) == PROVIDER_PRESET_KEYS, entry["providerId"]
            # Preset ids are provider-bound (both packaged and synthesized
            # preset ids equal their provider id); clients echo presetId back
            # verbatim through onboarding.provider.configure.
            assert preset["presetId"] == entry["providerId"]
            assert isinstance(preset["synthesized"], bool), entry["providerId"]
            # Preset tiers reuse the router-catalog tier payload shape so the
            # same client component renders both.
            tiers = preset["tiers"]
            assert set(FROZEN_TEXT_TIERS) <= set(tiers), entry["providerId"]
            for tier_name, tier in tiers.items():
                assert set(tier) == ROUTER_TIER_PAYLOAD_KEYS, (entry["providerId"], tier_name)
        # The entry-level defaultModel mirrors the (single) preset's.
        assert entry["defaultModel"] == presets[0]["defaultModel"], entry["providerId"]


def test_provider_catalog_preset_synthesized_flag_tracks_packaged_presets() -> None:
    # The legacy nine plus the curated-inline set ship packaged (curated)
    # presets; everything else is synthesized. A packaged preset silently
    # degrading to synthesized (or vice versa) changes what onboarding
    # clients offer to apply.
    packaged = FROZEN_ROUTER_PROFILE_IDS | FROZEN_CURATED_INLINE_PRESET_IDS
    entries = {entry["providerId"]: entry for entry in provider_catalog_payload()}
    for provider_id, entry in entries.items():
        expected_synthesized = provider_id not in packaged
        assert entry["presets"][0]["synthesized"] is expected_synthesized, provider_id


def test_router_catalog_top_level_shape_is_frozen() -> None:
    payload = router_catalog_payload()
    assert set(payload) == ROUTER_CATALOG_TOP_LEVEL_KEYS
    # Tier names are canonical c0-c3 and c1 is the default route; clients
    # (tier pills, onboarding tier tables) key directly off these strings.
    assert payload["textTiers"] == list(FROZEN_TEXT_TIERS)
    assert payload["defaultTier"] == "c1"


def test_router_catalog_modes_are_frozen() -> None:
    payload = router_catalog_payload()
    modes = payload["modes"]
    for mode in modes:
        assert set(mode) == ROUTER_MODE_KEYS
    # Mode ids are sent back verbatim by onboarding.router.configure clients.
    # "custom" is a deliberate addition: the provider-agnostic generalization
    # of openrouter-mix accepted for any active provider.
    assert {mode["mode"] for mode in modes} == {
        "recommended",
        "openrouter-mix",
        "custom",
        "disabled",
    }


def test_router_catalog_profiles_and_tier_payloads_are_frozen() -> None:
    payload = router_catalog_payload()
    profiles = payload["profiles"]

    profile_ids = {profile["profileId"] for profile in profiles}
    assert profile_ids == FROZEN_ROUTER_PROFILE_IDS
    # Keep the frozen wire set in lockstep with the source-of-truth constant;
    # if this fails, either the wire or the constant changed without the other.
    assert profile_ids == set(ROUTER_TIER_PROFILE_IDS)

    for profile in profiles:
        assert set(profile) == ROUTER_PROFILE_KEYS, profile.get("profileId")
        tiers = profile["tiers"]
        # Every profile must route all four text tiers; image_model is the
        # only other tier slot exposed to onboarding.
        assert set(FROZEN_TEXT_TIERS) <= set(tiers), profile["profileId"]
        assert set(tiers) <= set(FROZEN_TEXT_TIERS) | {"image_model"}, profile["profileId"]
        for tier_name, tier in tiers.items():
            assert set(tier) == ROUTER_TIER_PAYLOAD_KEYS, (profile["profileId"], tier_name)
