"""Async HTTP client for the AgentRMC native bridge (loopback only).

Consumes the existing AgentBridge contract without modifying it (PRD red line):
``GET /state``, ``POST /speak``, ``POST /notify``, ``GET /dispatch/pending``,
``POST /dispatch/ack``. Notify/speak succeed on ``204``; a non-204 or transport
error returns False so the caller can spool. ``dispatch/pending`` returns an
empty list when the route is absent (older native builds).

The client is loopback-only by construction (the configured bridge URL defaults
to 127.0.0.1) and never follows redirects off-host.
"""

from __future__ import annotations

from typing import Any

import httpx


class BridgeClient:
    """One reusable async client against the native bridge."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_ms: int = 5000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(max(0.5, timeout_ms / 1000.0))
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=transport,
            follow_redirects=False,
            # Loopback-only: never inherit environment proxies (a SOCKS/HTTP
            # proxy must not intercept the local pet bridge).
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def state(self) -> dict[str, Any] | None:
        """Return the pet state JSON, or None when unreachable."""
        try:
            response = await self._client.get("/state")
            if response.status_code != 200:
                return None
            value = response.json()
            return value if isinstance(value, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def speak(self, message: str, *, title: str = "") -> bool:
        body: dict[str, Any] = {"message": message}
        if title:
            body["title"] = title
        return await self._post_204("/speak", body)

    async def notify(self, payload: dict[str, Any]) -> bool:
        return await self._post_204("/notify", payload)

    async def _post_204(self, path: str, body: dict[str, Any]) -> bool:
        try:
            response = await self._client.post(path, json=body)
            return response.status_code == 204
        except httpx.HTTPError:
            return False

    async def dispatch_pending(self, exe: str) -> list[dict[str, Any]]:
        try:
            response = await self._client.get(
                "/dispatch/pending", params={"exe": exe}
            )
            if response.status_code != 200:
                return []
            value = response.json()
            if not isinstance(value, list):
                return []
            return [item for item in value if isinstance(item, dict)]
        except (httpx.HTTPError, ValueError):
            return []

    async def dispatch_ack(
        self, dispatch_id: str, *, ok: bool, error: str = ""
    ) -> bool:
        body: dict[str, Any] = {"dispatch_id": dispatch_id, "ok": ok}
        if error:
            body["error"] = error
        try:
            response = await self._client.post("/dispatch/ack", json=body)
            return response.status_code == 204
        except httpx.HTTPError:
            return False


__all__ = ["BridgeClient"]
