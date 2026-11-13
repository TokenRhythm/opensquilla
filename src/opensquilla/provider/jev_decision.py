"""Bounded non-chat transport for the TokenRhythm structured decision API."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from opensquilla.env import trust_env
from opensquilla.secrets import clean_header_secret

if TYPE_CHECKING:
    from opensquilla.provider.selector import ProviderConfig

JEV_MODEL = "NeoHorse-Jev-4B"
_MODEL_ALIASES = {"neohorse-jev", "neohorse-jev-4b", "neohorse-4b-jev"}
_MAX_RESPONSE_BYTES = 64 * 1024


def is_jev_model(model: str) -> bool:
    return model.strip().lower().removeprefix("tokenrhythm/") in _MODEL_ALIASES


class JevDecisionProvider(Protocol):
    async def decide(
        self, payload: dict[str, Any], *, timeout_seconds: float
    ) -> dict[str, Any]: ...


def _endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip("/") or "https://tokenrhythm.studio/v1"
    parts = urlsplit(base)
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("invalid_decision_endpoint")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("insecure_decision_endpoint")
    if not parts.path or parts.path == "/":
        base += "/v1"
    return base + "/decision"


class NativeJevClient:
    """Send one native decision request with no redirect or billable retry."""

    def __init__(self, config: ProviderConfig) -> None:
        if config.provider != "tokenrhythm":
            raise ValueError("invalid_decision_provider")
        self._endpoint = _endpoint(config.base_url)
        self._api_key = clean_header_secret(config.api_key, label="JEV API key")
        self._proxy = config.proxy
        if not self._api_key:
            raise ValueError("missing_decision_credential")

    async def decide(self, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        from opensquilla.desktop_account import account_http_client

        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > 1024 * 1024:
            raise ValueError("decision_request_too_large")
        # TLS and proxy initialization may block on some platforms.
        client = await asyncio.to_thread(
            account_http_client,
            self._api_key,
            timeout=timeout_seconds,
            trust_env=trust_env(),
            proxy=self._proxy or None,
            follow_redirects=False,
        )
        async with client:
            async with client.stream(
                "POST",
                self._endpoint,
                headers={
                    "Authorization": "Bearer " + self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                content=body,
            ) as response:
                response.raise_for_status()
                size = 0
                chunks: list[bytes] = []
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise ValueError("decision_response_too_large")
                    chunks.append(chunk)
                value = json.loads(b"".join(chunks))
                if not isinstance(value, dict):
                    raise ValueError("invalid_decision_response")
                return value
