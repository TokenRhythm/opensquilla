"""Opt-in live API matrix for the web retrieval stack.

These tests hit real public providers and public web pages only. They are
disabled by default and require both OPENSQUILLA_LIVE_SEARCH=1 and
OPENSQUILLA_LIVE_SEARCH_MATRIX=1 so the default CI suite only collects/skips
them.
"""

from __future__ import annotations

import inspect
import json
import os
from typing import Any, cast

import pytest
from typer.testing import CliRunner

import opensquilla.tools.builtin.web as web_module
from opensquilla.cli.main import app
from opensquilla.search.canonical import run_canonical_web_search
from opensquilla.search.runtime_config import SearchRuntimeConfig, resolve_search_runtime
from opensquilla.search.types import SearchOptions, SearchResult
from opensquilla.tools.builtin.web_fetch import _extract_inner, run_web_fetch_payload

pytestmark = pytest.mark.live_search

_QUERY = "Python release notes"
_PYTHON_DOMAIN = "python.org"

_PROVIDER_KEYS = {
    "duckduckgo": None,
    "tavily": "TAVILY_API_KEY",
    "brave": "BRAVE_SEARCH_API_KEY",
    "exa": "EXA_API_KEY",
    "iqs": "IQS_SEARCH_API_KEY",
    "bocha": "BOCHA_SEARCH_API_KEY",
}


def _require_live_matrix() -> None:
    if os.environ.get("OPENSQUILLA_LIVE_SEARCH") != "1":
        pytest.skip("set OPENSQUILLA_LIVE_SEARCH=1 to run live search tests")
    if os.environ.get("OPENSQUILLA_LIVE_SEARCH_MATRIX") != "1":
        pytest.skip("set OPENSQUILLA_LIVE_SEARCH_MATRIX=1 to run live search matrix")


def _require_env(name: str) -> None:
    if not os.environ.get(name):
        pytest.skip(f"{name} not set")


def _results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = payload.get("results")
    assert isinstance(results, list)
    return cast(list[dict[str, Any]], results)


def _domain_matches(domain: object, expected: str) -> bool:
    if not isinstance(domain, str):
        return False
    normalized = domain.lower().strip(".")
    expected = expected.lower().strip(".")
    return normalized == expected or normalized.endswith(f".{expected}")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(_PROVIDER_KEYS))
async def test_live_each_provider_searches_without_implicit_fetch(provider: str) -> None:
    _require_live_matrix()
    key = _PROVIDER_KEYS[provider]
    if key is not None:
        _require_env(key)

    async def unexpected_fetch(url: str, max_chars: int) -> dict[str, Any]:
        pytest.fail("default search must not fetch pages")

    payload = await run_canonical_web_search(
        SearchOptions(query=_QUERY, provider=provider),
        runtime=resolve_search_runtime(
            SearchRuntimeConfig(provider=provider, fallback_policy="off")
        ),
        fetcher=unexpected_fetch,
        use_cache=False,
    )
    # Keep failure diagnostics useful without dumping credentials or provider bodies.
    assert payload["ok"] is True, {
        "provider": provider,
        "error_kind": payload.get("error_kind"),
        "retry_allowed": payload.get("retry_allowed"),
    }
    results = _results(payload)
    assert results
    assert all(row["provider"] == provider and row["fetched"] is False for row in results)
    assert all(row["fetch_status"] == "not_requested" for row in results)
    assert payload["diagnostics"]["domain_limited_count"] == 0
    assert payload["provider_attempts"] == [{"provider": provider, "status": "success"}]


@pytest.mark.asyncio
async def test_live_tavily_canonical_web_search_enforces_domain_filter() -> None:
    _require_live_matrix()
    _require_env("TAVILY_API_KEY")

    payload = await run_canonical_web_search(
        SearchOptions(
            query=_QUERY,
            mode="technical",
            max_results=5,
            fetch_top_k=1,
            max_chars_per_source=1000,
            include_domains=(_PYTHON_DOMAIN,),
            provider="tavily",
        )
    )

    assert payload["ok"] is True
    results = _results(payload)
    assert results
    assert all(_domain_matches(result.get("domain"), _PYTHON_DOMAIN) for result in results)
    assert payload["provider_attempts"][0] == {"provider": "tavily", "status": "success"}
    assert payload["diagnostics"]["fetched_count"] <= 1


@pytest.mark.asyncio
async def test_live_web_search_tool_returns_bounded_json() -> None:
    _require_live_matrix()
    _require_env("TAVILY_API_KEY")

    bare_web_search = inspect.unwrap(web_module.web_search)
    raw = await bare_web_search(
        _QUERY,
        mode="technical",
        max_results=3,
        fetch_top_k=1,
        max_chars_per_source=800,
        include_domains=[_PYTHON_DOMAIN],
        provider="tavily",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    results = _results(payload)
    assert results
    assert all(_domain_matches(result.get("domain"), _PYTHON_DOMAIN) for result in results)
    assert all(len(str(result.get("excerpt") or "")) <= 800 for result in results)


@pytest.mark.asyncio
async def test_live_brave_provider_accepts_recency_filter() -> None:
    _require_live_matrix()
    _require_env("BRAVE_SEARCH_API_KEY")

    from opensquilla.search.providers.brave import BraveSearchProvider

    results = await BraveSearchProvider().search(_QUERY, max_results=3, recency="year")

    assert results
    assert all(isinstance(result, SearchResult) for result in results)
    assert results[0].provider == "brave"
    assert results[0].url.startswith("http")


@pytest.mark.asyncio
async def test_live_iqs_provider_accepts_recency_and_domain_filters() -> None:
    _require_live_matrix()
    _require_env("IQS_SEARCH_API_KEY")

    from opensquilla.search.providers.iqs import IqsSearchProvider

    results = await IqsSearchProvider().search(
        _QUERY,
        max_results=3,
        recency="year",
        include_domains=(_PYTHON_DOMAIN,),
    )

    assert results
    assert all(isinstance(result, SearchResult) for result in results)
    assert results[0].provider == "iqs"
    assert results[0].url.startswith("http")


@pytest.mark.asyncio
async def test_live_exa_canonical_web_search_returns_content_metadata() -> None:
    _require_live_matrix()
    _require_env("EXA_API_KEY")

    payload = await run_canonical_web_search(
        SearchOptions(
            query=_QUERY,
            mode="technical",
            max_results=3,
            fetch_top_k=0,
            max_chars_per_source=1000,
            include_domains=(_PYTHON_DOMAIN,),
            provider="exa",
        )
    )

    assert payload["ok"] is True
    assert payload["provider_attempts"][0] == {"provider": "exa", "status": "success"}
    results = _results(payload)
    assert results
    assert all(row.get("provider") == "exa" for row in results)
    assert all(_domain_matches(row.get("domain"), _PYTHON_DOMAIN) for row in results)
    assert any(str(row.get("excerpt") or "").strip() for row in results)


@pytest.mark.asyncio
async def test_live_web_fetch_extracts_public_python_homepage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_live_matrix()
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    payload = await run_web_fetch_payload(
        "https://www.python.org/",
        extract_mode="markdown",
        max_chars=1200,
    )

    assert 200 <= int(payload["status"]) < 300
    assert payload["extractor"] in {"readability", "html2text", "raw"}
    text = str(payload["text"])
    assert text.startswith('<external-content source="')
    assert "Python" in text
    assert payload["returned_length"] == len(_extract_inner(text)) <= 1200


@pytest.mark.asyncio
async def test_live_web_fetch_can_explicitly_use_firecrawl() -> None:
    _require_live_matrix()
    _require_env("FIRECRAWL_API_KEY")

    payload = await run_web_fetch_payload(
        "https://www.python.org/",
        extract_mode="markdown",
        max_chars=1200,
        extractor="firecrawl",
    )

    assert 200 <= int(payload["status"]) < 300
    assert payload["extractor"] == "firecrawl"
    text = str(payload["text"])
    assert text.startswith('<external-content source="')
    assert "Python" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("extractor", ["auto", "firecrawl"])
async def test_live_fetch_body_can_be_retrieved_after_preview(
    tmp_path, monkeypatch: pytest.MonkeyPatch, extractor: str,
) -> None:
    from types import SimpleNamespace

    from opensquilla.engine import Agent, AgentConfig
    from opensquilla.engine.tool_result_store import ToolResultStore
    from opensquilla.tools.builtin.tool_results import retrieve_tool_result
    from opensquilla.tools.builtin.web_fetch import _cache
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext, current_tool_context

    _require_live_matrix()
    if extractor == "firecrawl":
        _require_env("FIRECRAWL_API_KEY")
    else:
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    _cache.clear()
    registry = get_default_registry()
    context: ToolContext | None = ToolContext(
        is_owner=True, session_key="agent:main:live-search-test",
        allowed_tools={"retrieve_tool_result"},
    )
    agent = Agent(
        provider=cast(Any, SimpleNamespace(provider_name="unused")),
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "store"),
            tool_result_store_session_id="live-search-test",
            tool_result_store_session_key="agent:main:live-search-test",
            tool_result_store_agent_id="main",
        ),
        tool_context=context,
        tool_definitions=registry.to_tool_definitions(context),
        tool_handler=build_tool_handler(registry, context),
    )
    context = agent._tool_context
    assert context is not None
    context.tool_result_snapshot_writer = agent._write_tool_body_snapshot
    token = current_tool_context.set(context)
    try:
        payload = await run_web_fetch_payload(
            "https://www.python.org/", max_chars=200,
            extractor=extractor, _tool_use_id=f"live-fetch-{extractor}",
        )
        assert payload["status"] == 200
        assert payload["truncated"] is True
        assert payload["returned_length"] == len(_extract_inner(payload["text"])) <= 200
        recovery = payload["content_recovery"]
        assert recovery["available"] is True
        record = ToolResultStore(str(tmp_path / "store")).read(
            recovery["handle"], session_id="live-search-test",
        )
        args = recovery["next_call"]["arguments"]
        omitted = record.content[args["offset"]:args["offset"] + args["limit"]]
        head, marker, tail = _extract_inner(payload["text"]).partition(
            "\n[... middle omitted ...]\n"
        )
        assert marker
        body = _extract_inner(record.content)
        assert body.startswith(head) and body.endswith(tail)
        expected_middle = body[len(head):len(body) - len(tail)]
        assert expected_middle
        assert args["offset"] == record.content.find(">") + 1 + len(head)
        assert omitted == expected_middle[:args["limit"]]
        result = await inspect.unwrap(retrieve_tool_result)(**args)
        assert omitted in result
        assert all("content_recovery" not in entry for entry in _cache.values())
    finally:
        context.tool_result_snapshot_writer = None
        current_tool_context.reset(token)


def test_live_cli_canonical_web_search_query_returns_json() -> None:
    _require_live_matrix()
    _require_env("TAVILY_API_KEY")

    result = CliRunner().invoke(
        app,
        [
            "search",
            "query",
            _QUERY,
            "--provider",
            "tavily",
            "--mode",
            "technical",
            "--max-results",
            "3",
            "--fetch-top-k",
            "1",
            "--max-chars-per-source",
            "800",
            "--include-domain",
            _PYTHON_DOMAIN,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    results = _results(payload)
    assert results
    assert all(_domain_matches(row.get("domain"), _PYTHON_DOMAIN) for row in results)
