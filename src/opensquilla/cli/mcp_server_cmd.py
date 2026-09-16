"""CLI commands for running OpenSquilla as an inbound MCP server."""

from __future__ import annotations

import typer

from opensquilla.cli.url_utils import normalize_gateway_url

app = typer.Typer(help="Run the OpenSquilla MCP server bridge.")


@app.command("run")
def run_mcp_server(
    gateway_url: str | None = typer.Option(
        None,
        "--gateway",
        envvar="OPENSQUILLA_GATEWAY_URL",
        help=(
            "OpenSquilla gateway URL to bridge to. Defaults to the selected "
            "profile's configured gateway, or ws://localhost:18791/ws when none "
            "is configured."
        ),
    ),
) -> None:
    """Run a stdio MCP server exposing OpenSquilla session workflows."""

    from opensquilla.cli.gateway_rpc import default_gateway_url
    from opensquilla.mcp_server.bridge import OpenSquillaMCPBridge
    from opensquilla.mcp_server.server import create_mcp_server

    target_url = normalize_gateway_url(gateway_url) if gateway_url else default_gateway_url()
    bridge = OpenSquillaMCPBridge(gateway_url=target_url)
    try:
        mcp = create_mcp_server(bridge)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    mcp.run(transport="stdio")
