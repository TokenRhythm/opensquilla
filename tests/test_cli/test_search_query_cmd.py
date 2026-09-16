"""CLI tests for `opensquilla search query` canonical web search mode."""

from __future__ import annotations

import json

from typer.testing import CliRunner

import opensquilla.cli.search_cmd as search_cmd
from opensquilla.cli.main import app
from opensquilla.search.types import SearchOptions

runner = CliRunner()


def test_search_query_web_search_options_use_local_search(monkeypatch):
    seen_options: list[SearchOptions] = []

    async def fake_run_canonical_web_search(
        options: SearchOptions,
        **kwargs: object,
    ) -> dict[str, object]:
        seen_options.append(options)
        assert "fetcher" in kwargs
        assert options.fetch_top_k == 0
        return {
            "ok": True,
            "query": options.query,
            "mode": options.mode,
            "provider_attempts": [{"provider": "tavily", "status": "success"}],
            "diagnostics": {"returned_chars": 120},
            "results": [
                {
                    "rank": 1,
                    "title": "Python release",
                    "domain": "python.org",
                    "url": "https://www.python.org/downloads/",
                    "provider": "tavily",
                    "fetched": False,
                    "excerpt": "Python release notes",
                }
            ],
        }

    monkeypatch.setattr(
        search_cmd,
        "run_canonical_web_search",
        fake_run_canonical_web_search,
    )

    result = runner.invoke(
        app,
        [
            "search",
            "query",
            "python release",
            "--mode",
            "auto",
            "--fetch-top-k",
            "0",
            "--max-results",
            "5",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["query"] == "python release"
    assert payload["mode"] == "auto"
    assert payload["provider_attempts"] == [{"provider": "tavily", "status": "success"}]
    assert payload["diagnostics"] == {"returned_chars": 120}
    assert payload["results"][0]["title"] == "Python release"
    assert seen_options == [
        SearchOptions(query="python release", mode="auto", fetch_top_k=0, max_results=5)
    ]


def test_search_query_web_search_text_output_renders_results(monkeypatch):
    async def fake_run_canonical_web_search(
        options: SearchOptions,
        **kwargs: object,
    ) -> dict[str, object]:
        assert "fetcher" in kwargs
        assert options.fetch_top_k == 0
        return {
            "ok": True,
            "query": options.query,
            "mode": options.mode,
            "provider_attempts": [],
            "diagnostics": {},
            "results": [
                {
                    "rank": 1,
                    "title": "Python release notes",
                    "domain": "python.org",
                    "url": "https://www.python.org/downloads/",
                    "provider": "tavily",
                    "fetched": True,
                    "excerpt": "Python 3 release details.",
                }
            ],
        }

    monkeypatch.setattr(
        search_cmd,
        "run_canonical_web_search",
        fake_run_canonical_web_search,
    )

    result = runner.invoke(
        app,
        ["search", "query", "python release", "--fetch-top-k", "0"],
    )

    assert result.exit_code == 0, result.stdout
    assert "Python release notes" in result.stdout
    assert "python.org" in result.stdout
    assert "https://www.python.org/downloads/" in result.stdout


def test_search_query_web_search_json_failure_exits_nonzero(monkeypatch):
    async def fake_run_canonical_web_search(
        options: SearchOptions,
        **kwargs: object,
    ) -> dict[str, object]:
        assert "fetcher" in kwargs
        return {
            "ok": False,
            "query": options.query,
            "mode": options.mode,
            "provider_attempts": [{"provider": "tavily", "status": "error"}],
            "diagnostics": {},
            "results": [],
            "error": {"message": "network down"},
        }

    monkeypatch.setattr(
        search_cmd,
        "run_canonical_web_search",
        fake_run_canonical_web_search,
    )

    result = runner.invoke(
        app,
        ["search", "query", "python release", "--mode", "auto", "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["message"] == "network down"
