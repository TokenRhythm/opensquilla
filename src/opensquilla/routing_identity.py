"""Secret-free canonical identities shared by routing and provider layers."""

from __future__ import annotations


def canonicalize_provider_routing_upstream(value: object) -> str:
    """Return the stable, secret-free identity of one routing upstream.

    OpenRouter provider pins use a case-folded alphanumeric identity in
    ranking evidence. Health and persistent rollout state must use exactly the
    same shape so names such as ``google-ai-studio`` and ``googleaistudio`` do
    not create separate scopes.

    ``auto`` maps to the empty identity because a pre-dispatch caller cannot
    prove which upstream will serve the request. Credential identity is never
    part of this value.
    """

    raw = str(value or "").strip().casefold()
    if not raw or raw == "auto":
        return ""
    return "".join(character for character in raw if character.isalnum())


__all__ = ["canonicalize_provider_routing_upstream"]
