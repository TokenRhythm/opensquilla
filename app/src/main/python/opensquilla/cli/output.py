"""Small shared output helpers for scriptable CLI commands."""

from __future__ import annotations

import json
from typing import Any

import typer


def print_json(payload: Any) -> None:
    """Print JSON payload to stdout using the OpenSquilla CLI contract."""

    typer.echo(json.dumps(payload, ensure_ascii=False, default=str))


def error_payload(
    message: str,
    *,
    code: str | None = None,
    details: Any | None = None,
) -> dict[str, Any]:
    """Build the small OpenSquilla-owned JSON error envelope."""

    error: dict[str, Any] = {"message": message}
    if code:
        error["code"] = code
    if details is not None:
        error["details"] = details
    return {"error": error}


def emit_error(
    message: str,
    *,
    json_output: bool = False,
    code: str | None = None,
    details: Any | None = None,
) -> None:
    """Emit an error to stderr without polluting JSON stdout."""

    if json_output:
        typer.echo(
            json.dumps(
                error_payload(message, code=code, details=details),
                ensure_ascii=False,
                default=str,
            ),
            err=True,
        )
    else:
        typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
