"""Shared DRACO summary and Markdown rendering helpers."""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


def summarize_core(
    rows: list[dict[str, Any]],
    *,
    completed_quality_value_fn: Callable[..., Any],
    row_cost_accounting_fn: Callable[..., Any],
    merge_cost_accounting_fn: Callable[..., Any],
    row_usage_number_fn: Callable[..., Any],
    row_metric_int_fn: Callable[..., Any],
    row_server_tool_call_count_fn: Callable[..., Any],
    row_total_tool_call_count_fn: Callable[..., Any],
    row_trajectory_steps_fn: Callable[..., Any],
    row_llm_request_count_fn: Callable[..., Any],
    percentile_fn: Callable[..., Any],
    numeric_pct_delta_fn: Callable[..., Any],
) -> dict[str, Any]:
    completed_quality_value = completed_quality_value_fn
    row_cost_accounting = row_cost_accounting_fn
    merge_cost_accounting = merge_cost_accounting_fn
    row_usage_number = row_usage_number_fn
    row_metric_int = row_metric_int_fn
    row_server_tool_call_count = row_server_tool_call_count_fn
    row_total_tool_call_count = row_total_tool_call_count_fn
    row_trajectory_steps = row_trajectory_steps_fn
    row_llm_request_count = row_llm_request_count_fn
    percentile = percentile_fn
    numeric_pct_delta = numeric_pct_delta_fn

    summary: dict[str, Any] = {"groups": {}}
    judging_enabled = any(isinstance(row.get("judge"), dict) for row in rows)
    for group in sorted({row["group"] for row in rows}):
        group_rows = [row for row in rows if row["group"] == group]
        completed_rows = [row for row in group_rows if not row.get("error")]
        latencies = [int(row.get("latency_ms") or 0) for row in group_rows]
        scored_totals = [
            completed_quality_value(row)
            for row in completed_rows
            if row["quality_total"] is not None
        ]
        quality_values = (
            [completed_quality_value(row) for row in group_rows] if judging_enabled else []
        )
        pass_rates = [
            float((row.get("judge") or {}).get("pass_rate"))
            for row in completed_rows
            if isinstance((row.get("judge") or {}).get("pass_rate"), int | float)
        ]
        # Recompute with the current accounting contract. Persisted rows may
        # carry an older/stale cost_accounting object and must not bypass new
        # completeness gates.
        cost_accounts = [row_cost_accounting(row) for row in group_rows]
        completed_cost_accounts = [row_cost_accounting(row) for row in completed_rows]
        generation_costs = [
            float(account["generation"]["recorded_cost_usd"]) for account in cost_accounts
        ]
        actual_generation_costs = [
            float(account["actual_generation_spend"]["recorded_cost_usd"])
            for account in cost_accounts
        ]
        judge_costs = [float(account["judge"]["recorded_cost_usd"]) for account in cost_accounts]
        candidate_judge_costs = [
            float(account["candidate_judge"]["recorded_cost_usd"]) for account in cost_accounts
        ]
        costs = [float(account["recorded_total_cost_usd"]) for account in cost_accounts]
        actual_spend_costs = [
            float(account["actual_spend_recorded_total_cost_usd"]) for account in cost_accounts
        ]
        completed_costs = [
            float(account["recorded_total_cost_usd"]) for account in completed_cost_accounts
        ]
        group_llm_cost = merge_cost_accounting(
            "group_llm_total",
            [account["llm_total"] for account in cost_accounts],
        )
        actual_group_llm_cost = merge_cost_accounting(
            "actual_group_llm_total",
            [account["actual_llm_total"] for account in cost_accounts],
        )
        visible_tokens = [
            int(row_usage_number(row, "input_tokens")) + int(row_usage_number(row, "output_tokens"))
            for row in group_rows
        ]
        reasoning_tokens = [int(row_usage_number(row, "reasoning_tokens")) for row in group_rows]
        all_tokens = list(visible_tokens)
        stream_tool_calls = [
            row_metric_int(row, "stream_tool_call_count", "tool_call_count") for row in group_rows
        ]
        server_tool_calls = [row_server_tool_call_count(row) for row in group_rows]
        total_tool_calls = [row_total_tool_call_count(row) for row in group_rows]
        trajectory_steps = [row_trajectory_steps(row) for row in group_rows]
        llm_requests = [row_llm_request_count(row) for row in group_rows]
        usage_unknown = [
            int(account["llm_total"]["unknown_request_count"]) for account in cost_accounts
        ]
        summary["groups"][group] = {
            "rows": len(group_rows),
            "task_ids": sorted(str(row.get("task_id") or "") for row in group_rows),
            "completed": len(completed_rows),
            "scored_rows": len(scored_totals),
            "score_coverage_pct": (
                len(scored_totals) / len(group_rows) * 100.0 if group_rows else 0.0
            ),
            "avg_quality": statistics.mean(quality_values) if quality_values else None,
            "avg_quality_scored": (statistics.mean(scored_totals) if scored_totals else None),
            "avg_pass_rate": statistics.mean(pass_rates) if pass_rates else None,
            "judge_errors": sum(
                int((row.get("judge") or {}).get("judge_error_count") or 0)
                for row in completed_rows
            ),
            "avg_cost_usd": statistics.mean(costs) if costs else 0.0,
            "avg_cost_completed_usd": (
                statistics.mean(completed_costs) if completed_costs else None
            ),
            "recorded_total_cost_usd": sum(costs),
            "avg_actual_spend_cost_usd": (
                statistics.mean(actual_spend_costs) if actual_spend_costs else 0.0
            ),
            "actual_spend_recorded_total_cost_usd": sum(actual_spend_costs),
            "recorded_generation_cost_usd": sum(generation_costs),
            "actual_spend_generation_cost_usd": sum(actual_generation_costs),
            "recorded_judge_cost_usd": sum(judge_costs),
            "recorded_candidate_judge_cost_usd": sum(candidate_judge_costs),
            "avg_recorded_generation_cost_usd": (
                statistics.mean(generation_costs) if generation_costs else 0.0
            ),
            "avg_actual_spend_generation_cost_usd": (
                statistics.mean(actual_generation_costs) if actual_generation_costs else 0.0
            ),
            "avg_recorded_judge_cost_usd": (statistics.mean(judge_costs) if judge_costs else 0.0),
            "avg_recorded_candidate_judge_cost_usd": (
                statistics.mean(candidate_judge_costs) if candidate_judge_costs else 0.0
            ),
            "known_cost_request_coverage_pct": group_llm_cost["known_request_coverage_pct"],
            "exact_cost_request_coverage_pct": group_llm_cost["exact_request_coverage_pct"],
            "unknown_cost_request_count": group_llm_cost["unknown_request_count"],
            "unknown_cost_tokens": group_llm_cost["unknown_tokens"],
            "llm_cost_complete_rows": sum(
                1 for account in cost_accounts if account["llm_total"]["cost_complete"]
            ),
            "result_cost_complete_rows": sum(
                1 for account in cost_accounts if account["result_cost_complete"]
            ),
            "actual_spend_known_cost_request_coverage_pct": actual_group_llm_cost[
                "known_request_coverage_pct"
            ],
            "actual_spend_unknown_cost_request_count": actual_group_llm_cost[
                "unknown_request_count"
            ],
            "actual_spend_cost_complete_rows": sum(
                1 for account in cost_accounts if account["actual_spend_cost_complete"]
            ),
            "avg_visible_tokens": (statistics.mean(visible_tokens) if visible_tokens else 0.0),
            "avg_reasoning_tokens": (
                statistics.mean(reasoning_tokens) if reasoning_tokens else 0.0
            ),
            "avg_total_tokens": statistics.mean(all_tokens) if all_tokens else 0.0,
            "avg_stream_tool_calls": (
                statistics.mean(stream_tool_calls) if stream_tool_calls else 0.0
            ),
            "avg_server_tool_calls": (
                statistics.mean(server_tool_calls) if server_tool_calls else 0.0
            ),
            "avg_tool_calls": (statistics.mean(total_tool_calls) if total_tool_calls else 0.0),
            "total_tool_calls": sum(total_tool_calls),
            "tool_call_rate_pct": (
                sum(1 for count in total_tool_calls if count > 0) / len(total_tool_calls) * 100.0
                if total_tool_calls
                else 0.0
            ),
            "avg_trajectory_steps": (
                statistics.mean(trajectory_steps) if trajectory_steps else 0.0
            ),
            "avg_llm_requests": (statistics.mean(llm_requests) if llm_requests else 0.0),
            "total_llm_requests": sum(llm_requests),
            "avg_usage_unknown": (statistics.mean(usage_unknown) if usage_unknown else 0.0),
            "total_usage_unknown": sum(usage_unknown),
            "latency_p50_ms": percentile(latencies, 50),
            "latency_p95_ms": percentile(latencies, 95),
        }
    for item in summary["groups"].values():
        for baseline in ("B0", "B1"):
            baseline_item = summary["groups"].get(baseline) or {}
            suffix = baseline.lower()
            item[f"avg_quality_pct_delta_vs_{suffix}"] = numeric_pct_delta(
                item.get("avg_quality"),
                baseline_item.get("avg_quality"),
            )
            comparable_costs = (
                item.get("result_cost_complete_rows") == item.get("rows")
                and baseline_item.get("result_cost_complete_rows") == baseline_item.get("rows")
                and item.get("completed") == item.get("rows")
                and baseline_item.get("completed") == baseline_item.get("rows")
                and item.get("task_ids") == baseline_item.get("task_ids")
            )
            item[f"avg_cost_pct_delta_vs_{suffix}"] = (
                numeric_pct_delta(
                    item.get("avg_cost_usd"),
                    baseline_item.get("avg_cost_usd"),
                )
                if comparable_costs
                else None
            )
    return summary


def render_markdown_core(
    summary: dict[str, Any],
    jsonl_path: Path,
    tool_policy: dict[str, Any] | None,
    generation_policy: dict[str, Any] | None,
    runner_mode: str,
    agent_max_iterations: int,
    agent_finalization_policy: Mapping[str, Any] | None,
    *,
    benchmark_tool_policy_fn: Callable[..., Any],
    generation_thinking_policy_fn: Callable[..., Any],
    normalized_agent_finalization_policy_fn: Callable[..., Any],
    runner_mode_name: str,
) -> str:
    benchmark_tool_policy = benchmark_tool_policy_fn
    generation_thinking_policy = generation_thinking_policy_fn
    normalized_agent_finalization_policy = normalized_agent_finalization_policy_fn
    RUNNER_MODE = runner_mode_name  # noqa: N806 - exact extracted body alias

    stamp = jsonl_path.stem.removeprefix("draco_ensemble_")
    trace_path = jsonl_path.parent / f"draco_run_{stamp}.trace.jsonl"
    policy = tool_policy or benchmark_tool_policy()
    generation = generation_policy or generation_thinking_policy()
    finalization = normalized_agent_finalization_policy(agent_finalization_policy)
    blocked_domains = policy.get("contamination_blocked_domains") or []
    tool_line = (
        f"Runner mode: `{runner_mode}`; tool mode: `{policy.get('tool_mode') or RUNNER_MODE}`; "
        "tools enabled: "
        f"`{str(bool(policy.get('tools_enabled'))).lower()}`"
    )
    if policy.get("tools_enabled"):
        tool_names = ", ".join(str(name) for name in policy.get("tool_names") or [])
        if tool_names:
            tool_line = f"{tool_line}; tools: `{tool_names}`."
        else:
            tool_line = f"{tool_line}."
    if not policy.get("tools_enabled"):
        tool_line = (
            f"Runner mode: `{runner_mode}`; tool mode: "
            f"`{policy.get('tool_mode') or RUNNER_MODE}`; "
            "external research tools are not attached."
        )
    group_tool_policies = policy.get("group_tool_policies") or {}
    fusion_groups = [
        str(group)
        for group, group_policy in group_tool_policies.items()
        if isinstance(group_policy, dict) and group_policy.get("openrouter_fusion_enabled")
    ]
    fusion_line = ""
    if fusion_groups:
        if not policy.get("tools_enabled"):
            tool_line = (
                f"Runner mode: `{runner_mode}`; tool mode: "
                f"`{policy.get('tool_mode') or RUNNER_MODE}`; "
                "no global external research tools are attached."
            )
        fusion_line = (
            "OpenRouter Fusion groups: "
            f"`{', '.join(sorted(fusion_groups))}` use only `openrouter:fusion` "
            "with `tool_choice=required`; Fusion's internal web_search/web_fetch "
            "domain controls are not exposed in the documented tool parameters."
        )
    generation_budget_note = f"budget: `{generation.get('thinking_budget_tokens')}`"
    if generation.get("max_thinking_budget_tokens") is not None:
        generation_budget_note = (
            f"{generation_budget_note}, "
            f"max budget: `{generation.get('max_thinking_budget_tokens')}`"
        )

    def _signed_pct(value: Any) -> str:
        return f"{float(value):+.2f}%" if isinstance(value, int | float) else ""

    lines = [
        "# DRACO Ensemble Summary",
        "",
        f"Raw JSONL: `{jsonl_path}`",
        f"Trace JSONL: `{trace_path}`",
        "",
        "Generation thinking: "
        f"`{generation.get('generation_thinking')}` "
        f"(enabled: `{generation.get('thinking_enabled')}`, "
        f"level: `{generation.get('thinking_level')}`, "
        f"{generation_budget_note}, "
        f"temperature: `{generation.get('temperature')}`).",
        f"Agent max iterations: `{agent_max_iterations}`.",
        "Agent finalization policy: "
        f"`{json.dumps(finalization, ensure_ascii=False, sort_keys=True)}`.",
        tool_line,
        *([fusion_line] if fusion_line else []),
        "Contamination blocked domains: "
        f"`{', '.join(blocked_domains) if blocked_domains else '(none)'}`.",
        "Cost accounting: selected columns contain only the accepted generation attempt "
        "plus rubric/candidate Judge attempts; actual-spend columns contain every "
        "generation attempt plus those Judge attempts. Unpriced requests remain unknown "
        "rather than being treated as $0; selected cost deltas are blank unless both "
        "groups are complete. Preflight and replaced/failed shard spend must still be "
        "audited at the whole-experiment level.",
        "",
        "| Group | Rows | Done | Avg Quality | AvgQ Scored | Avg Pass | "
        "Judge Err | Avg Selected LLM $ | Avg Selected Gen $ | Avg Actual LLM $ | "
        "Avg Actual Gen $ | Avg Judge $ | Selected Known Cost % | Actual Known Cost % | "
        "Selected Complete | Actual Complete | Avg Visible | Avg Reason | Avg Tokens | "
        "Avg Tools | Tool % | "
        "Avg Steps | Avg LLM Req | Unknown Cost Req | p50 ms | p95 ms | "
        "AvgQ % vs B0 | Avg$ % vs B0 | "
        "AvgQ % vs B1 | Avg$ % vs B1 |",
        "| --- |" + " ---: |" * 29,
    ]
    for group, item in sorted(summary["groups"].items()):
        lines.append(
            "| {group} | {rows} | {done} | {quality} | {quality_scored} | "
            "{pass_rate} | {judge_errors} | {cost:.6f} | {generation_cost:.6f} | "
            "{actual_cost:.6f} | {actual_generation_cost:.6f} | {judge_cost:.6f} | "
            "{known_cost:.1f}% | {actual_known_cost:.1f}% | "
            "{cost_complete}/{rows} | {actual_cost_complete}/{rows} | "
            "{visible_tokens:.1f} | {reasoning_tokens:.1f} | "
            "{tokens:.1f} | {tool_calls:.1f} | {tool_rate:.1f}% | "
            "{steps:.1f} | {llm_requests:.1f} | {usage_unknown:.1f} | "
            "{p50:.0f} | {p95:.0f} | "
            "{q_b0} | {cost_b0} | {q_b1} | {cost_b1} |".format(
                group=group,
                rows=item["rows"],
                done=item["completed"],
                quality=(f"{item['avg_quality']:.2f}" if item["avg_quality"] is not None else ""),
                quality_scored=(
                    f"{item['avg_quality_scored']:.2f}"
                    if item["avg_quality_scored"] is not None
                    else ""
                ),
                pass_rate=(
                    f"{item['avg_pass_rate']:.2f}" if item["avg_pass_rate"] is not None else ""
                ),
                judge_errors=item["judge_errors"],
                cost=item["avg_cost_usd"],
                generation_cost=item["avg_recorded_generation_cost_usd"],
                actual_cost=item["avg_actual_spend_cost_usd"],
                actual_generation_cost=item["avg_actual_spend_generation_cost_usd"],
                judge_cost=(
                    item["avg_recorded_judge_cost_usd"]
                    + item["avg_recorded_candidate_judge_cost_usd"]
                ),
                known_cost=item["known_cost_request_coverage_pct"],
                actual_known_cost=item["actual_spend_known_cost_request_coverage_pct"],
                cost_complete=item["result_cost_complete_rows"],
                actual_cost_complete=item["actual_spend_cost_complete_rows"],
                visible_tokens=item["avg_visible_tokens"],
                reasoning_tokens=item["avg_reasoning_tokens"],
                tokens=item["avg_total_tokens"],
                tool_calls=item["avg_tool_calls"],
                tool_rate=item["tool_call_rate_pct"],
                steps=item["avg_trajectory_steps"],
                llm_requests=item["avg_llm_requests"],
                usage_unknown=item["avg_usage_unknown"],
                p50=item["latency_p50_ms"],
                p95=item["latency_p95_ms"],
                q_b0=_signed_pct(item.get("avg_quality_pct_delta_vs_b0")),
                cost_b0=_signed_pct(item.get("avg_cost_pct_delta_vs_b0")),
                q_b1=_signed_pct(item.get("avg_quality_pct_delta_vs_b1")),
                cost_b1=_signed_pct(item.get("avg_cost_pct_delta_vs_b1")),
            )
        )
    return "\n".join(lines) + "\n"
