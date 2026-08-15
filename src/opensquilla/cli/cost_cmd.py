"""Usage/cost CLI commands."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from typing import Any, cast

import typer
from rich.table import Table

from opensquilla.cli.gateway_rpc import run_gateway_sync
from opensquilla.cli.output import print_json
from opensquilla.cli.ui import ACCENT_HEADER, console

app = typer.Typer(help="Inspect usage and estimated cost.")


def _usage_values(row: Mapping[str, Any]) -> tuple[int, int, float]:
    return (
        int(row.get("input_tokens") or row.get("inputTokens") or 0),
        int(row.get("output_tokens") or row.get("outputTokens") or 0),
        float(row.get("cost_usd") or row.get("costUsd") or 0.0),
    )


def _model_rows_for_aggregation(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    breakdown = (
        row.get("deploymentBreakdown")
        or row.get("deployment_breakdown")
        or row.get("modelBreakdown")
        or row.get("model_breakdown")
    )
    if breakdown is None or breakdown == []:
        return [row]

    parent_input, parent_output, parent_cost = _usage_values(row)
    if not isinstance(breakdown, list) or not all(
        isinstance(item, Mapping) for item in breakdown
    ):
        return [
            {
                "model": "unknown",
                "inputTokens": parent_input,
                "outputTokens": parent_output,
                "costUsd": parent_cost,
            }
        ]

    model_rows = cast(list[Mapping[str, Any]], breakdown)
    child_values = [_usage_values(model_row) for model_row in model_rows]
    breakdown_input = sum(values[0] for values in child_values)
    breakdown_output = sum(values[1] for values in child_values)
    breakdown_cost = sum(values[2] for values in child_values)
    cost_tolerance = 1e-6 * max(1, len(model_rows))
    is_complete = (
        breakdown_input == parent_input
        and breakdown_output == parent_output
        and math.isclose(
            breakdown_cost,
            parent_cost,
            rel_tol=1e-9,
            abs_tol=cost_tolerance,
        )
    )
    if is_complete:
        return model_rows

    return [
        {
            "model": "unknown",
            "inputTokens": parent_input,
            "outputTokens": parent_output,
            "costUsd": parent_cost,
        }
    ]


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
            for model_row in _model_rows_for_aggregation(row):
                model = model_row.get("model") or "unknown"
                input_tokens, output_tokens, cost_usd = _usage_values(model_row)
                grouped[model]["input"] += input_tokens
                grouped[model]["output"] += output_tokens
                grouped[model]["cost"] += cost_usd
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
