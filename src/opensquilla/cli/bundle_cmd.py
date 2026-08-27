"""``opensquilla bundle`` — collect a redacted diagnostics bundle."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from opensquilla.cli.output import emit_error, print_json


def _live_enrichment() -> dict[str, Any]:
    """Best-effort doctor/channels snapshots from a running gateway.

    Any failure (gateway down, auth, timeout) returns {} silently — the
    bundle must work identically with a dead gateway.
    """

    async def _fetch() -> dict[str, Any]:
        from opensquilla.cli import gateway_client as gateway_client_module
        from opensquilla.cli.gateway_rpc import default_gateway_url

        client = gateway_client_module.GatewayClient()
        extra: dict[str, Any] = {}
        try:
            # The rest of the bundle is read from the selected profile's home.
            # Enriching it from a hardcoded 127.0.0.1:18791 meant a bundle
            # collected under `--profile x` could carry `doctor`/`channels`
            # sections describing a different gateway, and nothing in the file
            # said so — worse than having no live section at all (#1374).
            await client.connect(default_gateway_url())
            for key, method in (("doctor", "doctor.status"), ("channels", "channels.status")):
                try:
                    extra[key] = dict(await client.call(method, {}))
                except Exception:  # noqa: BLE001 - enrichment is optional
                    pass
        finally:
            await client.close()
        return extra

    try:
        return asyncio.run(_fetch())
    except KeyboardInterrupt:
        raise
    except BaseException:  # noqa: BLE001 - includes SystemExit from connect()
        return {}


def bundle_command(
    output: Path | None = typer.Option(
        None, "--output", help="Bundle destination (default: ./opensquilla-bundle-<UTC>.zip)."
    ),
    days: int = typer.Option(3, "--days", min=1, help="How many days of logs to include."),
    session: str | None = typer.Option(None, "--session", help="Focus on one session id."),
    include_content: bool = typer.Option(
        False,
        "--include-content",
        help="Include conversation content (raw turn-call capture). Off by default.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Collect logs, error records, and redacted config into one shareable zip."""
    from opensquilla.observability.bundle import collect_bundle

    if output is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = Path.cwd() / f"opensquilla-bundle-{stamp}.zip"

    try:
        # collect_bundle (and the enrichment client) log best-effort failures
        # via structlog, whose default PrintLogger writes to stdout; keep
        # stdout reserved for the command's own output (especially --json).
        with contextlib.redirect_stdout(sys.stderr):
            extra = _live_enrichment()
            result = collect_bundle(
                output,
                days=days,
                session_id=session,
                include_content=include_content,
                extra=extra or None,
            )
    except Exception as exc:  # noqa: BLE001 - only zip-creation failures reach here
        emit_error(str(exc), json_output=json_output, code="BUNDLE_FAILED")
        raise typer.Exit(1) from exc

    if json_output:
        print_json({"path": str(result.path), **result.manifest})
    else:
        typer.echo(f"Diagnostics bundle written to {result.path}")
        typer.echo("Attach this file to your GitHub issue (it is redacted by default).")
        if result.manifest.get("collection_errors"):
            typer.echo(
                f"Note: {len(result.manifest['collection_errors'])} artifact(s) "
                "could not be collected; see manifest.json inside the bundle."
            )
