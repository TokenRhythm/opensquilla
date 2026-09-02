"""Provider account balance / remaining-credits lookup.

This module answers "how much credit is left on the configured provider
account?" — a proactive counterpart to ``usage.query`` (which reports what has
already been spent) and to the reactive ``INSUFFICIENT_CREDITS`` provider
failure (which only surfaces after a request fails and then parks the
credential on a long cooldown).

The public entry point is :func:`query_provider_balance`, consumed by the
``balance.query`` RPC handler.  It is intentionally provider-aware but
degrades gracefully: providers without a documented balance endpoint return an
``unsupported`` status rather than raising, so the UI can always render a
sensible answer.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from opensquilla.provider.credentials import (
    credential_provider_hint,
    endpoint_provider_hint,
)

# Providers with a documented, machine-readable balance/credits endpoint.
# TokenRhythm is intentionally absent: at the time of writing it exposes no
# public balance route (``/api/*`` account paths return 404), so it is treated
# as ``unsupported`` until such an endpoint is published. Add it here once the
# route and payload shape are known.
_SUPPORTED_BALANCE_PROVIDERS: frozenset[str] = frozenset({"openrouter"})

_DEFAULT_TIMEOUT_SECONDS = 15.0


class BalanceQueryError(RuntimeError):
    """Raised when a supported provider's balance lookup cannot complete.

    Distinct from the ``unsupported`` result, which is a normal (non-error)
    outcome returned in the response payload.
    """


def _resolve_api_key(api_key: str, api_key_env: str) -> str:
    """Resolve the effective API key: explicit value first, else env var."""

    key = str(api_key or "").strip()
    if key:
        return key
    env_name = str(api_key_env or "").strip()
    if env_name:
        return str(os.environ.get(env_name, "")).strip()
    return ""


def _resolve_provider_id(provider: str, base_url: str, api_key: str, api_key_env: str) -> str:
    """Best-effort provider id from explicit name, else endpoint/credential hints.

    The configured ``provider`` field wins. When it is a generic value (e.g.
    ``custom``) but the endpoint or key clearly belongs to a known provider,
    fall back to that hint so a re-hosted OpenRouter still resolves correctly.
    """

    explicit = str(provider or "").strip().lower()
    if explicit in _SUPPORTED_BALANCE_PROVIDERS:
        return explicit
    hint = endpoint_provider_hint(base_url) or credential_provider_hint(
        api_key, api_key_env=api_key_env
    )
    if hint:
        return hint
    return explicit


def _unsupported(provider_id: str, reason: str) -> dict[str, Any]:
    return {
        "provider": provider_id,
        "status": "unsupported",
        "reason": reason,
        "balance": None,
        "unit": None,
        "currency": None,
    }


async def _query_openrouter(
    base_url: str,
    api_key: str,
    *,
    proxy: str | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Query OpenRouter's ``GET /credits`` endpoint.

    OpenRouter returns ``{"data": {"total_credits": X, "total_usage": Y}}``;
    the remaining balance is ``total_credits - total_usage``. See
    https://openrouter.ai/docs/api-reference/get-credits.

    ``transport`` is a test seam (e.g. ``httpx.MockTransport``); production
    call sites leave it ``None``.
    """

    root = str(base_url or "https://openrouter.ai/api/v1").rstrip("/")
    url = f"{root}/credits"
    headers = {"Authorization": f"Bearer {api_key}"}
    # httpx forbids passing both ``proxy`` and ``transport``; the transport
    # (tests) takes precedence and needs no proxy.
    client_kwargs: dict[str, Any] = {"timeout": _DEFAULT_TIMEOUT_SECONDS}
    if transport is not None:
        client_kwargs["transport"] = transport
    else:
        client_kwargs["proxy"] = proxy
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:  # network / timeout / connection error
        raise BalanceQueryError(f"OpenRouter balance request failed: {exc}") from exc

    if resp.status_code != 200:
        raise BalanceQueryError(
            f"OpenRouter balance request returned HTTP {resp.status_code}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise BalanceQueryError("OpenRouter balance response was not JSON") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise BalanceQueryError("OpenRouter balance response missing 'data' object")

    total_credits = _coerce_float(data.get("total_credits"))
    total_usage = _coerce_float(data.get("total_usage"))
    if total_credits is None or total_usage is None:
        raise BalanceQueryError(
            "OpenRouter balance response missing total_credits/total_usage"
        )
    balance = round(total_credits - total_usage, 6)
    return {
        "provider": "openrouter",
        "status": "ok",
        "balance": balance,
        "totalCredits": total_credits,
        "totalUsage": total_usage,
        "unit": "credits",
        "currency": "USD",
    }


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


async def query_provider_balance(
    *,
    provider: str,
    base_url: str,
    api_key: str = "",
    api_key_env: str = "",
    proxy: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Return the configured provider's remaining balance.

    Resolves the effective provider id and credential, dispatches to the
    matching provider client, and returns a normalized payload. Providers
    without a documented balance endpoint yield ``status="unsupported"``;
    supported providers that fail to answer raise :class:`BalanceQueryError`.

    ``transport`` is a test seam forwarded to the provider client; production
    call sites leave it ``None``.
    """

    provider_id = _resolve_provider_id(provider, base_url, api_key, api_key_env)

    if provider_id not in _SUPPORTED_BALANCE_PROVIDERS:
        return _unsupported(
            provider_id or str(provider or "").strip().lower(),
            "This provider does not expose a balance query endpoint.",
        )

    key = _resolve_api_key(api_key, api_key_env)
    if not key:
        raise BalanceQueryError(
            f"No API key configured for provider '{provider_id}'."
        )

    if provider_id == "openrouter":
        return await _query_openrouter(base_url, key, proxy=proxy, transport=transport)

    # Unreachable given the membership check above, but keeps the dispatch
    # exhaustive if _SUPPORTED_BALANCE_PROVIDERS grows without a branch.
    return _unsupported(provider_id, "No balance client implemented for this provider.")
