"""Secret-aware redaction helpers used by mutations, RPC, and CLI output."""

from __future__ import annotations

import re
from typing import Any

from opensquilla.onboarding.channel_specs import get_channel_setup_spec

# Re-export: the free-text primitive lives in the dependency-free
# ``opensquilla.redaction`` module because ``opensquilla.provider.failures``
# imports it and importing ``opensquilla.onboarding`` transitively imports
# the provider package (a cycle if it lived here).
from opensquilla.redaction import redact_error_text as redact_error_text

REDACTED_PLACEHOLDER = "***"

_PROVIDER_SECRET_FIELDS = frozenset({"api_key"})


def is_redacted_secret_sentinel(value: object) -> bool:
    """True when a secret value is a round-tripped redaction mask.

    Every redacted payload echo in this module uses ``REDACTED_PLACEHOLDER``,
    and status surfaces render all-asterisk masks of varying width, so any
    all-asterisk string can only be a display value a client read back —
    never a real credential. Mutation and probe paths treat it server-side as
    "keep the stored secret" (or reject it when nothing is stored), the same
    trust boundary the channel-secret merge enforces, so a read-modify-write
    RPC/CLI client cannot destroy a stored key by echoing its mask.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and set(text) == {"*"}


def redact_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    for key in _PROVIDER_SECRET_FIELDS:
        if key in out and out[key]:
            out[key] = REDACTED_PLACEHOLDER
    return out


# "key" is safe here as an EXACT name (key/KEY/Key) — the ambiguity concern
# (monkey) only applies to suffix matching, which stays "_key"-anchored.
_TIER_SECRET_EXACT_KEYS = frozenset(
    {"key", "api_key", "token", "secret", "password", "authorization"}
)
_TIER_SECRET_SUFFIXES = ("_key", "_token", "_secret", "_password")
# Separator-free fallback for acronym runs no boundary rule can split
# (APIKEY). Deliberately excludes the bare "key" suffix so ordinary words
# (monkey) are not redacted.
_TIER_SECRET_SQUASHED_SUFFIXES = ("apikey", "token", "secret", "password", "authorization")
# apiKey -> api_Key
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# APIKey -> API_Key
_ACRONYM_BOUNDARY_RE = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


def _is_secret_like_tier_key(key: str) -> bool:
    """Match secret-shaped keys in any spelling (api_key, apiKey, APIKey, APIKEY).

    Tier dicts are untyped RPC payloads, so only the three known display
    aliases get canonicalized to snake_case on write — any other spelling
    passes through verbatim. Normalize camel/acronym case boundaries and
    separators before matching, with a separator-free fallback, so the
    redaction cannot be dodged by spelling.
    """
    with_boundaries = _ACRONYM_BOUNDARY_RE.sub("_", str(key))
    with_boundaries = _CAMEL_BOUNDARY_RE.sub("_", with_boundaries)
    normalized = with_boundaries.replace("-", "_").lower()
    if normalized in _TIER_SECRET_EXACT_KEYS or normalized.endswith(_TIER_SECRET_SUFFIXES):
        return True
    squashed = normalized.replace("_", "")
    return squashed.endswith(_TIER_SECRET_SQUASHED_SUFFIXES)


def redact_router_tiers_payload(tiers: dict[str, Any]) -> dict[str, Any]:
    """Redact secret-like fields hand-written into router tier dicts.

    Tiers are untyped dicts and carry no secrets by design (credentials live
    in ``[llm_profiles.<id>]``), but nothing stops an operator from pasting
    an ``api_key`` into one — the router-configure RPC response must not
    echo it back. (Adversarial-review finding salvaged from PR #406.)
    """
    out: dict[str, Any] = {}
    for tier_name, tier in tiers.items():
        if not isinstance(tier, dict):
            out[tier_name] = tier
            continue
        out[tier_name] = {
            key: (REDACTED_PLACEHOLDER if _is_secret_like_tier_key(key) and value else value)
            for key, value in tier.items()
        }
    return out


def redact_search_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if out.get("api_key"):
        out["api_key"] = REDACTED_PLACEHOLDER
    return out


def redact_image_generation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if out.get("api_key"):
        out["api_key"] = REDACTED_PLACEHOLDER
    return out


def redact_audio_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if out.get("api_key"):
        out["api_key"] = REDACTED_PLACEHOLDER
    return out


def redact_memory_embedding_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if out.get("api_key"):
        out["api_key"] = REDACTED_PLACEHOLDER
    remote = out.get("remote")
    if isinstance(remote, dict) and remote.get("api_key"):
        remote = dict(remote)
        remote["api_key"] = REDACTED_PLACEHOLDER
        out["remote"] = remote
    return out


def redact_channel_entry(type_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        spec = get_channel_setup_spec(type_name)
    except KeyError:
        # Fail closed for types without a setup spec (entry-point plugin
        # adapters): without declared secret fields, redact anything
        # secret-shaped rather than returning credentials verbatim.
        return {
            key: (REDACTED_PLACEHOLDER if _is_secret_like_tier_key(key) and value else value)
            for key, value in payload.items()
        }
    secret_names = {f.name for f in spec.fields if f.secret}
    out = dict(payload)
    for key in secret_names:
        if key in out and out[key]:
            out[key] = REDACTED_PLACEHOLDER
    return out
