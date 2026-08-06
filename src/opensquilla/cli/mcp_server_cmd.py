"""CLI commands for running OpenSquilla as an inbound MCP server."""

from __future__ import annotations

import typer

from opensquilla.cli.url_utils import normalize_gateway_url

app = typer.Typer(help="Run the OpenSquilla MCP server bridge.")


@app.command("run")
def run_mcp_server(
    gateway_url: str = typer.Option(
        "ws://localhost:18791/ws",
        "--gateway",
        envvar="OPENSQUILLA_GATEWAY_URL",
        help="OpenSquilla gateway URL to bridge to.",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="OPENSQUILLA_GATEWAY_TOKEN",
        help="Gateway auth token (defaults to OPENSQUILLA_GATEWAY_TOKEN or config [auth].token).",
    ),
) -> None:
    """Run a stdio MCP server exposing OpenSquilla session workflows."""

    from opensquilla.mcp_server.bridge import OpenSquillaMCPBridge
    from opensquilla.mcp_server.server import create_mcp_server

    bridge = OpenSquillaMCPBridge(
        gateway_url=normalize_gateway_url(gateway_url),
        auth_token=token,
    )
    try:
        mcp = create_mcp_server(bridge)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    mcp.run(transport="stdio")
