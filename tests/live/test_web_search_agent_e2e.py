"""Opt-in live agent E2E gate for canonical source-backed web_search.

The prompt and assertions use public current-facts search only. The test skips
unless live search and live LLM credentials are explicitly present.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any, cast

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.types import ToolCall
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.search.canonical import run_canonical_web_search
from opensquilla.search.types import SearchOptions

pytestmark = [pytest.mark.live_search, pytest.mark.llm, pytest.mark.llm_tools]


def _require_live_agent_search() -> None:
    if os.environ.get("OPENSQUILLA_LIVE_SEARCH") != "1":
        pytest.skip("set OPENSQUILLA_LIVE_SEARCH=1 to run live search tests")
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")
    if not os.environ.get("TAVILY_API_KEY"):
        pytest.skip("TAVILY_API_KEY not set")


def _tool_def(name: str, description: str, properties: dict[str, Any]) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=ToolInputSchema(properties=properties, required=list(properties)),
    )


def _normalize_query(query: str) -> str:
    return " ".join(query.lower().strip().split())


def _answer_cites_source_url(text: str, urls: set[str]) -> bool:
    cited_urls = {url for url in urls if url}
    cited_urls.update(url.rstrip("/") for url in urls if url.rstrip("/"))
    return any(url in text for url in cited_urls)


@pytest.mark.asyncio
async def test_live_agent_uses_web_search_without_web_fetch_loop() -> None:
    _require_live_agent_search()

    calls: Counter[str] = Counter()
    web_search_queries: list[str] = []
    source_result_urls: set[str] = set()

    async def tool_handler(call: ToolCall) -> ToolResult:
        calls[call.tool_name] += 1
        if call.tool_name == "web_search":
            query = str(call.arguments.get("query") or "Python latest release notes")
            web_search_queries.append(query)
            payload = await run_canonical_web_search(
                SearchOptions(
                    query=query,
                    mode="news",
                    max_results=5,
                    fetch_top_k=1,
                    max_chars_per_source=1200,
                    provider="tavily",
                )
            )
            results = payload.get("results")
            if isinstance(results, list):
                for result in results:
                    if not isinstance(result, dict):
                        continue
                    url = result.get("url")
                    if isinstance(url, str):
                        source_result_urls.add(url)
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=json.dumps(payload, ensure_ascii=True),
                is_error=not bool(payload.get("ok")),
            )

        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="Tool is intentionally unavailable in this live gate.",
            is_error=True,
        )

    provider = OpenAIProvider(
        api_key=cast(str, os.environ.get("OPENROUTER_API_KEY")),
        model=os.environ.get("LLM_TEST_MODEL", "openai/gpt-4o-mini"),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        provider_kind="openrouter",
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            max_tokens=256,
            request_timeout=45.0,
            timeout=90.0,
            tool_timeout=30.0,
            temperature=0.0,
        ),
        tool_definitions=[
            _tool_def(
                "web_search",
                "Run one normalized web search and return compact cited results.",
                {"query": {"type": "string"}},
            ),
            _tool_def(
                "web_fetch",
                "Fetch a single URL.",
                {"url": {"type": "string"}},
            ),
        ],
        tool_handler=tool_handler,
    )

    events = [
        event
        async for event in agent.run_turn(
            "Find the current Python release notes using the available web tools when "
            "current information is needed. Prefer the tool that returns compact "
            "citation-ready search results. Answer in one sentence with one source URL."
        )
    ]
    text = "\n".join(str(getattr(event, "text", "")) for event in events)
    normalized_web_search_queries = [_normalize_query(query) for query in web_search_queries]
    repeated_web_search_queries = [
        query
        for query, count in Counter(normalized_web_search_queries).items()
        if query and count > 1
    ]

    assert calls["web_search"] == 1
    assert calls["web_fetch"] == 0
    assert repeated_web_search_queries == []
    assert _answer_cites_source_url(text, source_result_urls)
