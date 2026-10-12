"""Usage/cost CLI commands."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, cast

import typer
from rich.table import Table

from opensquilla.cli.gateway_rpc import run_gateway_sync
from opensquilla.cli.output import print_json
from opensquilla.cli.ui import ACCENT_HEADER, console

app = typer.Typer(help="Inspect usage and estimated cost.")


@app.callback(invoke_without_command=True)
def cost(
    by_model: bool = typer.Option(False, "--by-model", help="Group aggregate rows by model"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show aggregate usage/cost from the running gateway."""

    async def _run(client) -> dict[Any, Any]:
        return cast(dict[Any, Any], await client.usage_cost())

    payload = run_gateway_sync(_run, json_output=json_output)

    rows = payload.get("breakdown", [])
    if by_model:
        grouped: dict[str, dict[str, float]] = defaultdict(
            lambda: {"input": 0, "output": 0, "cost": 0.0}
        )
        for row in rows:
            model = row.get("model") or "unknown"
            grouped[model]["input"] += int(row.get("input_tokens") or row.get("inputTokens") or 0)
            grouped[model]["output"] += int(
                row.get("output_tokens") or row.get("outputTokens") or 0
            )
            grouped[model]["cost"] += float(row.get("cost_usd") or row.get("costUsd") or 0.0)
        if json_output:
            print_json(
                {
                    "byModel": [
                        {
                            "model": model,
                            "inputTokens": int(data["input"]),
                            "outputTokens": int(data["output"]),
                            "costUsd": data["cost"],
                        }
                        for model, data in sorted(grouped.items())
                    ],
                    "totalCostUsd": payload.get("totalCostUsd"),
                }
            )
            return
        table = Table(title="Cost by Model", show_header=True, header_style=ACCENT_HEADER)
        table.add_column("Model")
        table.add_column("Input", justify="right")
        table.add_column("Output", justify="right")
        table.add_column("Cost", justify="right")
        for model, data in sorted(grouped.items()):
            table.add_row(
                model,
                f"{int(data['input']):,}",
                f"{int(data['output']):,}",
                f"${data['cost']:.6f}",
            )
        console.print(table)
        return

    if json_output:
        print_json(payload)
        return

    table = Table(title="Cost", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Session")
    table.add_column("Model")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cost", justify="right")
    for row in rows:
        table.add_row(
            str(row.get("session") or row.get("sessionKey") or ""),
            str(row.get("model") or ""),
            f"{int(row.get('input_tokens') or row.get('inputTokens') or 0):,}",
            f"{int(row.get('output_tokens') or row.get('outputTokens') or 0):,}",
            f"${float(row.get('cost_usd') or row.get('costUsd') or 0.0):.6f}",
        )
    console.print(table)
    console.print(f"[dim]total: ${float(payload.get('totalCostUsd') or 0.0):.6f}[/dim]")
