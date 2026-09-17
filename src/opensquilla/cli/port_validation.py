"""Shared, input-free diagnostics for gateway port configuration."""

from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from opensquilla.gateway.config import GatewayPort

GATEWAY_PORT_MESSAGE = (
    "Gateway port must be an integer between 0 and 65535. "
    "Fix port in the config file or OPENSQUILLA_GATEWAY_PORT."
)
_PORT_ADAPTER = TypeAdapter(GatewayPort)


def validate_gateway_port(value: object) -> int:
    return _PORT_ADAPTER.validate_python(value)


def has_gateway_port_error(error: ValidationError) -> bool:
    # Never render Pydantic's input/context: another invalid field can contain
    # credentials, and even the port input may be an accidentally pasted secret.
    return any(
        item["loc"] == ("port",)
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    )
