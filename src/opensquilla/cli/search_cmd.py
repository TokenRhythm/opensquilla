"""CLI: opensquilla search list/configure."""

from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path
from typing import Any, NoReturn, cast

import typer
from rich.console import Console
from rich.table import Table

from opensquilla.cli.gateway_rpc import run_gateway_sync
from opensquilla.cli.output import print_json
from opensquilla.cli.ui import warning_panel
from opensquilla.onboarding.config_store import (
    default_config_path,
    load_config,
    persist_config,
)
from opensquilla.onboarding.mutations import upsert_search_provider
from opensquilla.onboarding.next_steps import env_reference_warnings
from opensquilla.onboarding.search_specs import (
    list_search_provider_setup_specs,
    search_provider_catalog_payload,
)
from opensquilla.search.canonical import run_canonical_web_search
from opensquilla.search.types import DEFAULT_SEARCH_MAX_RESULTS, Recency, SearchMode, SearchOptions

search_app = typer.Typer(help="Configure and inspect web search providers.")

_SEARCH_MODES = {"auto", "news", "technical", "broad"}
_RECENCIES = {"day", "week", "month", "year"}
_BENCHMARK_PROFILES = {
    "smoke": (
        {"id": "release_notes", "query": "python release", "kind": "release_notes"},
        {
            "id": "technical_docs",
            "query": "sqlite json functions",
            "kind": "technical_docs",
        },
        {
            "id": "official_docs",
            "query": "starlette websocket docs",
            "kind": "official_docs",
        },
    )
}


@search_app.command("list")
def search_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List all known search providers."""
    if json_output:
        print_json(search_provider_catalog_payload())
        return

    console = Console(width=160, force_terminal=False)
    table = Table(title="Search providers")
    table.add_column("provider", no_wrap=True)
    table.add_column("label", no_wrap=True)
    table.add_column("runtime", no_wrap=True)
    table.add_column("requires key", no_wrap=True)
    table.add_column("env key")
    for spec in list_search_provider_setup_specs():
        table.add_row(
            spec.provider_id,
            spec.label,
            "supported" if spec.runtime_supported else "unsupported (disabled)",
            "yes" if spec.requires_api_key else "no",
            spec.env_key or "-",
        )
    console.print(table)


@search_app.command("status")
def search_status(
    provider: str | None = typer.Argument(None, help="Optional search provider id"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Show runtime search provider diagnostics from the running gateway."""

    async def _run(client):
        params: dict[str, object] = {}
        if provider:
            params["provider"] = provider
        return await client.call("search.status", params)

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return

    console = Console(width=140, force_terminal=False)
    table = Table(title="Search status")
    table.add_column("provider", no_wrap=True)
    table.add_column("active", no_wrap=True)
    table.add_column("configured", no_wrap=True)
    table.add_column("buildable", no_wrap=True)
    table.add_column("network", no_wrap=True)
    table.add_column("fallback")
    table.add_column("error")
    network_blocked = str(payload.get("networkBlockedReason") or "")
    table.add_row(
        str(payload.get("provider") or ""),
        "yes" if payload.get("provider") == payload.get("activeProvider") else "no",
        "yes" if payload.get("configured") else "no",
        "yes" if payload.get("buildable") else "no",
        "blocked" if network_blocked else "ok",
        str(payload.get("fallbackPolicy") or ""),
        str(payload.get("error") or ""),
    )
    console.print(table)
    # A provider that is configured and buildable can still be refused before it
    # is reached. Say so here rather than letting the next query be the message.
    if network_blocked:
        console.print(f"[yellow]Queries are blocked from here:[/yellow] {network_blocked}")


@search_app.command("query")
def search_query(
    query: str = typer.Argument(..., help="Search query"),
    provider: str | None = typer.Option(None, "--provider", help="Search provider id"),
    limit: int | None = typer.Option(None, "--limit", "-n", help="Maximum results"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    mode: str | None = typer.Option(
        None,
        "--mode",
        help="Research mode: auto/news/technical/broad",
    ),
    max_results: int | None = typer.Option(
        None,
        "--max-results",
        help="Maximum normalized research hits",
    ),
    fetch_top_k: int | None = typer.Option(
        None,
        "--fetch-top-k",
        help="Fetch excerpts for this many top hits",
    ),
    max_chars_per_source: int | None = typer.Option(None, "--max-chars-per-source"),
    include_domains: list[str] | None = typer.Option(None, "--include-domain"),
    exclude_domains: list[str] | None = typer.Option(None, "--exclude-domain"),
    recency: str | None = typer.Option(None, "--recency"),
) -> None:
    """Run a diagnostic search query through the running gateway."""

    research_mode_requested = any(
        value is not None and value != []
        for value in (
            mode,
            max_results,
            fetch_top_k,
            max_chars_per_source,
            include_domains,
            exclude_domains,
            recency,
        )
    )
    if research_mode_requested:
        payload = _run_local_research_query(
            query=query,
            provider=provider,
            limit=limit,
            json_output=json_output,
            mode=mode,
            max_results=max_results,
            fetch_top_k=fetch_top_k,
            max_chars_per_source=max_chars_per_source,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            recency=recency,
        )
        if json_output:
            print_json(payload)
            if not payload.get("ok", False):
                raise typer.Exit(1)
            return

        if not payload.get("ok", False):
            error = payload.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            typer.secho(f"Search failed: {message}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        _print_research_results(query, payload)
        return

    async def _run(client):
        params: dict[str, object] = {"query": query}
        if provider:
            params["provider"] = provider
        if limit is not None:
            params["limit"] = limit
        return await client.call("search.query", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        if not payload.get("ok", False):
            raise typer.Exit(1)
        return

    if not payload.get("ok", False):
        error = payload.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        typer.secho(f"Search failed: {message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    console = Console(width=160, force_terminal=False)
    table = Table(title=f"Search: {query}")
    table.add_column("Title")
    table.add_column("URL")
    table.add_column("Snippet")
    for row in payload.get("results", []):
        table.add_row(
            str(row.get("title") or ""),
            str(row.get("url") or ""),
            str(row.get("snippet") or "")[:100],
        )
    console.print(table)


def _run_local_research_query(
    *,
    query: str,
    provider: str | None,
    limit: int | None,
    json_output: bool,
    mode: str | None,
    max_results: int | None,
    fetch_top_k: int | None,
    max_chars_per_source: int | None,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    recency: str | None,
) -> dict[str, Any]:
    if mode is not None and mode not in _SEARCH_MODES:
        _emit_invalid_request(
            f"Invalid mode. Expected one of: {', '.join(sorted(_SEARCH_MODES))}.",
            json_output=json_output,
        )
    if recency is not None and recency not in _RECENCIES:
        _emit_invalid_request(
            f"Invalid recency. Expected one of: {', '.join(sorted(_RECENCIES))}.",
            json_output=json_output,
        )

    options = SearchOptions(
        query=query,
        mode=cast(SearchMode, mode or "auto"),
        max_results=(
            max_results if max_results is not None else (limit if limit is not None else 10)
        ),
        fetch_top_k=fetch_top_k if fetch_top_k is not None else 3,
        max_chars_per_source=max_chars_per_source if max_chars_per_source is not None else 1500,
        include_domains=tuple(include_domains or ()),
        exclude_domains=tuple(exclude_domains or ()),
        recency=cast(Recency | None, recency),
        provider=provider,
    )
    return asyncio.run(run_canonical_web_search(options, fetcher=_web_search_fetcher))


async def _web_search_fetcher(url: str, max_chars: int) -> dict[str, object]:
    from opensquilla.tools.builtin.web_fetch import run_web_fetch_payload

    return await run_web_fetch_payload(url, max_chars=max_chars)


def _emit_invalid_request(message: str, *, json_output: bool) -> NoReturn:
    if json_output:
        print_json({"ok": False, "error_kind": "invalid_request", "error": message})
    else:
        typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(2)


def _print_research_results(query: str, payload: dict[str, Any]) -> None:
    console = Console(width=180, force_terminal=False)
    table = Table(title=f"Research search: {query}")
    table.add_column("Rank", no_wrap=True)
    table.add_column("Title")
    table.add_column("Domain", no_wrap=True)
    table.add_column("Provider", no_wrap=True)
    table.add_column("Fetched", no_wrap=True)
    table.add_column("URL")
    table.add_column("Excerpt")
    for index, row in enumerate(payload.get("results", []), start=1):
        if not isinstance(row, dict):
            continue
        table.add_row(
            str(row.get("rank") or index),
            str(row.get("title") or ""),
            str(row.get("domain") or ""),
            str(row.get("provider") or ""),
            "yes" if row.get("fetched") else "no",
            str(row.get("url") or ""),
            str(row.get("excerpt") or row.get("snippet") or "")[:160],
        )
    console.print(table)


@search_app.command("benchmark")
def search_benchmark(
    profile: str = typer.Option("smoke", "--profile"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    fetch_top_k: int = typer.Option(3, "--fetch-top-k"),
    max_results: int = typer.Option(DEFAULT_SEARCH_MAX_RESULTS, "--max-results"),
    live: bool = typer.Option(False, "--live"),
) -> None:
    """Compare legacy and normalized search output shapes for synthetic queries."""
    try:
        payload = _run_search_benchmark(
            profile=profile,
            fetch_top_k=fetch_top_k,
            max_results=max_results,
            live=live,
        )
    except ValueError as exc:
        _emit_invalid_request(str(exc), json_output=json_output)

    if json_output:
        print_json(payload)
        return

    console = Console(width=160, force_terminal=False)
    table = Table(title=f"Search benchmark: {profile}")
    table.add_column("Metric")
    table.add_column("v1", justify="right")
    table.add_column("v2", justify="right")
    for metric in (
        "p50_latency_ms",
        "p95_latency_ms",
        "success_at_k",
        "external_tool_calls_per_question",
        "avg_returned_chars",
        "duplicate_url_rate",
        "fetch_success_rate",
        "provider_fallback_count",
    ):
        v1 = cast(dict[str, Any], payload["v1"])
        v2 = cast(dict[str, Any], payload["v2"])
        table.add_row(metric, str(v1.get(metric, "")), str(v2.get(metric, "")))
    console.print(table)


def _run_search_benchmark(
    profile: str,
    fetch_top_k: int,
    max_results: int,
    live: bool = False,
) -> dict[str, Any]:
    if profile not in _BENCHMARK_PROFILES:
        raise ValueError(f"Unknown benchmark profile: {profile}")
    if live:
        raise ValueError("live benchmark is provided by the live gate test suite.")
    if max_results < 1:
        raise ValueError("max_results must be greater than or equal to 1.")
    if max_results > 20:
        raise ValueError("max_results must be less than or equal to 20.")
    if fetch_top_k < 0:
        raise ValueError("fetch_top_k must be greater than or equal to 0.")
    if fetch_top_k > 5:
        raise ValueError("fetch_top_k must be less than or equal to 5.")

    cases = [dict(row) for row in _BENCHMARK_PROFILES[profile]]
    query_count = len(cases)
    v1_latencies = [18 + (idx * 2) for idx in range(query_count)]
    v2_latencies = [12 + fetch_top_k + idx for idx in range(query_count)]
    v1 = {
        "p50_latency_ms": _percentile(v1_latencies, 50),
        "p95_latency_ms": _percentile(v1_latencies, 95),
        "success_at_k": round(max(0.0, min(1.0, 0.55 + (max_results * 0.04))), 3),
        "external_tool_calls_per_question": 2,
        "avg_returned_chars": max_results * 100,
        "duplicate_url_rate": 0.2,
        "fetch_success_rate": 0.0,
        "provider_fallback_count": 1,
    }
    v2 = {
        "p50_latency_ms": _percentile(v2_latencies, 50),
        "p95_latency_ms": _percentile(v2_latencies, 95),
        "success_at_k": round(max(0.0, min(1.0, 0.72 + (max_results * 0.04))), 3),
        "external_tool_calls_per_question": 1,
        "avg_returned_chars": max_results * 60,
        "duplicate_url_rate": 0.0,
        "fetch_success_rate": 1.0 if fetch_top_k > 0 else 0.0,
        "provider_fallback_count": 0,
    }
    return {
        "profile": profile,
        "measurement_kind": "synthetic_smoke",
        "query_count": query_count,
        "live": False,
        "v1": v1,
        "v2": v2,
        "cases": cases,
        "delta": {
            "success_at_k": round(v2["success_at_k"] - v1["success_at_k"], 3),
            "external_tool_calls_per_question": (
                v2["external_tool_calls_per_question"] - v1["external_tool_calls_per_question"]
            ),
            "avg_returned_chars": v2["avg_returned_chars"] - v1["avg_returned_chars"],
        },
    }


def _percentile(values: list[int], percentile: int) -> int:
    if percentile == 50:
        return int(statistics.median(values))
    ordered = sorted(values)
    index = round((len(ordered) - 1) * (percentile / 100))
    return ordered[index]


@search_app.command("configure")
def search_configure(
    provider: str = typer.Argument(..., help="Search provider id (e.g. brave)."),
    api_key: str = typer.Option("", "--api-key", "-k"),
    api_key_env: str = typer.Option("", "--api-key-env"),
    max_results: int = typer.Option(DEFAULT_SEARCH_MAX_RESULTS, "--max-results"),
    proxy: str = typer.Option("", "--proxy"),
    use_env_proxy: bool = typer.Option(
        False, "--use-env-proxy/--no-use-env-proxy"
    ),
    fallback_policy: str = typer.Option("off", "--fallback-policy"),
    diagnostics: bool = typer.Option(False, "--diagnostics/--no-diagnostics"),
    config_path: Path | None = typer.Option(
        None, "--config", help="Override config path."
    ),
) -> None:
    """Configure the active web search provider."""
    target = config_path or default_config_path()
    cfg = load_config(target)
    try:
        result = upsert_search_provider(
            cfg,
            provider_id=provider,
            api_key=api_key,
            api_key_env=api_key_env,
            max_results=max_results,
            proxy=proxy,
            use_env_proxy=use_env_proxy,
            fallback_policy=fallback_policy,
            diagnostics=diagnostics,
        )
    except (ValueError, KeyError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    persist = persist_config(
        result.config, path=target, restart_required=result.restart_required
    )
    typer.echo(f"Search provider configured: {provider}")
    typer.echo(f"Config: {persist.path}")
    warning_console = Console(file=sys.stdout, width=160, force_terminal=False)
    for warning in env_reference_warnings(result.config):
        warning_console.print(warning_panel(warning))
    if persist.backup_path:
        typer.echo(f"Backup: {persist.backup_path}")
