"""multi-search-engine skill — load + missing-key engines fail soft."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from opensquilla.skills.eligibility import EligibilityContext, check_eligibility
from opensquilla.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "opensquilla" / "skills" / "bundled"
SCRIPTS = BUNDLED / "multi-search-engine" / "scripts"


def _search_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import search  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return search


def _spec() -> object:
    return SkillLoader(bundled_dir=BUNDLED).get_by_name("multi-search-engine")


def test_skill_loads() -> None:
    spec = _spec()
    assert spec is not None
    assert spec.name == "multi-search-engine"


def test_eligibility_with_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "opensquilla.skills.eligibility.shutil.which",
        lambda name: "/usr/bin/python3" if name in {"python", "python3"} else None,
    )
    spec = _spec()
    assert spec is not None
    assert check_eligibility(spec, EligibilityContext.auto())


def test_brave_without_key_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine missing its API key must not crash the run; record an error and continue."""
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    search = _search_module()

    payload = search.search_all(
        query="anything",
        engines=["brave"],
        limit=3,
        strict=False,
    )
    assert payload["query"] == "anything"
    assert payload["results"] == []
    assert any("BRAVE_SEARCH_API_KEY/BRAVE_API_KEY" in e["reason"] for e in payload["errors"])


def test_brave_accepts_current_search_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenSquilla config uses BRAVE_SEARCH_API_KEY; the skill must honor it."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-current")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    search = _search_module()

    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "web": {
                    "results": [
                        {
                            "title": "Example",
                            "url": "https://example.com",
                            "description": "Snippet",
                        },
                    ],
                },
            }

    class _Client:
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, *, params: dict[str, object], headers: dict[str, str]) -> _Response:
            captured["headers"] = headers
            captured["params"] = params
            return _Response()

    monkeypatch.setattr(search, "_client", lambda: _Client())

    payload = search.search_all(
        query="anything",
        engines=["brave"],
        limit=1,
        strict=False,
    )

    assert payload["errors"] == []
    assert payload["results"][0]["url"] == "https://example.com"
    assert captured["headers"]["X-Subscription-Token"] == "brave-current"


def test_search_query_contract_extracts_planner_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Meta report planners may pass SEARCH_QUERY plus preferences; engines get only the query."""
    search = _search_module()

    captured: dict[str, object] = {}

    def fake_engine(query: str, limit: int) -> list[object]:
        captured["query"] = query
        captured["limit"] = limit
        return []

    monkeypatch.setitem(search.ENGINES, "fake", fake_engine)
    payload = search.search_all(
        query=(
            "SEARCH_QUERY: local-first AI coding assistants 2026 pros cons\n"
            "AUDIENCE: CTO\n"
            "REPORT_TYPE: technical"
        ),
        engines=["fake"],
        limit=7,
        strict=False,
    )

    assert payload["query"] == "local-first AI coding assistants 2026 pros cons"
    assert captured == {
        "query": "local-first AI coding assistants 2026 pros cons",
        "limit": 7,
    }


def test_unknown_engine_recorded() -> None:
    search = _search_module()

    payload = search.search_all(
        query="x",
        engines=["bogus-engine-name"],
        limit=1,
        strict=False,
    )
    assert payload["results"] == []
    assert any("unknown engine" in e["reason"] for e in payload["errors"])


def test_crossref_uses_bibliographic_query_and_preserves_real_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = _search_module()
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "message": {
                    "items": [
                        {
                            "DOI": "10.5555/Example.One",
                            "title": ["Resource-aware task routing"],
                            "container-title": ["Journal of Edge Systems"],
                            "published-print": {"date-parts": [[2022, 7]]},
                            "author": [
                                {"given": "Ada", "family": "Lovelace"},
                                {"literal": "Edge Systems Consortium"},
                            ],
                        },
                        {
                            "DOI": "10.5555/example.two",
                            "title": ["A second paper"],
                            "issued": {"date-parts": [[2021]]},
                            "author": [{"given": "Lin", "family": "Chen"}],
                        },
                        {
                            "DOI": "10.5555/ignored.by.limit",
                            "title": ["Third"],
                        },
                    ]
                }
            }

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(
            self,
            url: str,
            *,
            params: dict[str, object],
            headers: dict[str, str],
        ) -> _Response:
            captured.update(url=url, params=params, headers=headers)
            return _Response()

    monkeypatch.setenv("CROSSREF_MAILTO", "research@example.invalid")
    monkeypatch.setattr(search, "_client", lambda: _Client())

    payload = search.search_all(
        query="resource-aware routing (site:arxiv.org OR site:dl.acm.org)",
        engines=["crossref"],
        limit=2,
        strict=False,
    )

    assert payload["errors"] == []
    assert captured["url"] == "https://api.crossref.org/v1/works"
    assert captured["params"] == {
        "query.bibliographic": "resource-aware routing",
        "rows": 2,
        "mailto": "research@example.invalid",
    }
    assert len(payload["results"]) == 2
    first = payload["results"][0]
    assert first == {
        "engine": "crossref",
        "title": "Resource-aware task routing",
        "url": "https://doi.org/10.5555/example.one",
        "snippet": "Journal of Edge Systems",
        "rank": 1,
        "doi": "10.5555/example.one",
        "year": 2022,
        "authors": ["Ada Lovelace", "Edge Systems Consortium"],
        "corporate_authors": ["Edge Systems Consortium"],
    }


def test_crossref_year_never_uses_metadata_registration_date() -> None:
    search = _search_module()

    assert search._crossref_year({"created": {"date-parts": [[2026, 7, 20]]}}) is None
    assert search._crossref_year({"published": {"date-parts": [[2019, 3]]}}) == 2019
    assert search._crossref_year({"posted": {"date-parts": [[2018, 11]]}}) == 2018


def test_tavily_clamps_result_limit_to_api_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    search = _search_module()
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"results": []}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, *, json: dict[str, object]) -> _Response:
            captured.update(url=url, json=json)
            return _Response()

    monkeypatch.setenv("TAVILY_API_KEY", "tavily-fixture")
    monkeypatch.setattr(search, "_client", lambda: _Client())

    assert search._tavily_search("edge routing", 30) == []
    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["json"]["max_results"] == 20


@pytest.mark.parametrize("transient_status", [429, 500, 503])
def test_request_retries_transient_http_statuses_only_finitely(
    monkeypatch: pytest.MonkeyPatch,
    transient_status: int,
) -> None:
    search = _search_module()
    calls: list[int] = []

    class _Response:
        headers: dict[str, str] = {}

        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "transient",
                    request=httpx.Request("GET", "https://example.invalid"),
                    response=httpx.Response(self.status_code),
                )

        def close(self) -> None:
            return None

    class _Client:
        def get(self, url: str) -> _Response:
            calls.append(len(calls) + 1)
            return _Response(transient_status)

    monkeypatch.setattr(search.time, "sleep", lambda _: None)
    with pytest.raises(httpx.HTTPStatusError):
        search._request(_Client(), "get", "https://example.invalid")
    assert len(calls) == search.MAX_ATTEMPTS == 3


def test_request_retries_timeouts_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    search = _search_module()
    calls = 0

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def get(self, url: str) -> _Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ReadTimeout("fixture timeout")
            return _Response()

    monkeypatch.setattr(search.time, "sleep", lambda _: None)
    response = search._request(_Client(), "get", "https://example.invalid")
    assert response.status_code == 200
    assert calls == 3


def test_retry_delay_honors_crossref_rate_limit_interval() -> None:
    search = _search_module()

    class _Response:
        headers = {"X-Rate-Limit-Interval": "1s"}

    assert search._retry_delay(_Response(), 0) == 1.0


def test_aggregate_deduplicates_by_doi_arxiv_and_normalized_url_stably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = _search_module()

    def first(query: str, limit: int):
        return [
            search.Result(
                engine="first",
                title="DOI winner",
                url="https://doi.org/10.1000/DUPLICATE",
                snippet="",
                rank=1,
                doi="10.1000/duplicate",
            ),
            search.Result(
                engine="first",
                title="arXiv winner",
                url="https://arxiv.org/abs/2401.12345v2",
                snippet="",
                rank=2,
            ),
            search.Result(
                engine="first",
                title="URL winner",
                url="http://www.example.org/paper/?b=2&utm_source=test&a=1#section",
                snippet="",
                rank=3,
            ),
        ]

    def second(query: str, limit: int):
        return [
            search.Result(
                engine="second",
                title="Duplicate DOI",
                url="https://publisher.example/doi/10.1000/duplicate",
                snippet="",
                rank=1,
            ),
            search.Result(
                engine="second",
                title="Duplicate arXiv",
                url="https://arxiv.org/pdf/2401.12345.pdf",
                snippet="",
                rank=2,
            ),
            search.Result(
                engine="second",
                title="Duplicate URL",
                url="https://example.org/paper?a=1&b=2",
                snippet="",
                rank=3,
            ),
            search.Result(
                engine="second",
                title="Unique",
                url="https://example.org/unique",
                snippet="",
                rank=4,
            ),
        ]

    monkeypatch.setitem(search.ENGINES, "first", first)
    monkeypatch.setitem(search.ENGINES, "second", second)

    payload = search.search_all("papers", ["first", "second"], 20, strict=False)
    assert [item["title"] for item in payload["results"]] == [
        "DOI winner",
        "arXiv winner",
        "URL winner",
        "Unique",
    ]


def test_strict_mode_stops_at_first_error_and_cli_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    search = _search_module()
    calls: list[str] = []

    def failing(query: str, limit: int):
        calls.append("failing")
        raise RuntimeError("fixture failure")

    def must_not_run(query: str, limit: int):
        calls.append("must-not-run")
        return []

    monkeypatch.setitem(search.ENGINES, "failing", failing)
    monkeypatch.setitem(search.ENGINES, "must-not-run", must_not_run)
    payload = search.search_all(
        "papers",
        ["failing", "must-not-run"],
        5,
        strict=True,
    )
    assert calls == ["failing"]
    assert payload["errors"] == [{"engine": "failing", "reason": "fixture failure"}]

    monkeypatch.setattr(
        sys,
        "argv",
        ["search.py", "--query", "papers", "--engines", "unknown", "--strict"],
    )
    assert search.main() == 1
    assert json.loads(capsys.readouterr().out)["errors"][0]["engine"] == "unknown"
