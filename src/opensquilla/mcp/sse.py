"""Official SDK HTTP+SSE transport with OpenSquilla's network policy."""

from __future__ import annotations

from typing import Any

from opensquilla import __version__
from opensquilla.env import trust_env as _trust_env
from opensquilla.mcp.sdk_client import SDKMCPClient


class MCPSSEClient(SDKMCPClient):
    """Consume the server's endpoint event using the SDK's origin checks."""

    def _make_sdk_client(self) -> Any:
        if not self.config.url:
            raise ValueError("SSE transport requires a URL")
        if self.config.message_endpoint is not None:
            raise ValueError(
                "MCP SSE message_endpoint overrides are unsupported; "
                "the server must publish an endpoint event"
            )
        import httpx2
        from mcp import Client
        from mcp.client.sse import sse_client
        from mcp.types import Implementation

        def make_http_client(
            headers: Any = None, timeout: Any = None, auth: Any = None,
        ) -> Any:
            # The SDK supplies the operation timeouts. An idle SSE stream has
            # always been allowed to stay open indefinitely in OpenSquilla.
            operation_timeout = httpx2.Timeout(timeout)
            operation_timeout.read = None
            return httpx2.AsyncClient(
                headers=headers,
                auth=auth,
                timeout=operation_timeout,
                trust_env=_trust_env(),
            )

        return Client(
            sse_client(
                self.config.url,
                timeout=self.config.tool_timeout_seconds,
                httpx_client_factory=make_http_client,
            ),
            mode="auto",
            cache=None,
            read_timeout_seconds=self.config.tool_timeout_seconds,
            client_info=Implementation(name="opensquilla", version=__version__),
        )
