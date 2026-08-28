"""``opensquilla dist`` subcommand - emit workspace-state.json.

Emits a reproducible, versioned inventory of the current opensquilla install:
bundled channels, bundled tools, gateway safety defaults, package metadata, and
the package's Python requirement.
"""

from __future__ import annotations

from pathlib import Path

import typer

from opensquilla.dist.workspace_state import to_json

app = typer.Typer(
    help=(
        "Emit workspace-state.json - a reproducible, versioned inventory "
        "of this OpenSquilla install."
    ),
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def dist(
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the workspace-state.json payload to this file instead of stdout.",
    ),
) -> None:
    """Emit the workspace-state.json payload.

    With no flags, prints the payload to stdout. With ``--output <path>``,
    writes the payload to the given file and prints the resolved path to
    stdout so the operator can capture it from shell pipelines.
    """

    payload = to_json()
    if output is None:
        typer.echo(payload, nl=False)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
    typer.echo(str(output))
