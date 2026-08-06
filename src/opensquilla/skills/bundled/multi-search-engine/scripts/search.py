"""Query multiple search engines and emit a normalized JSON result list."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "OpenSquilla-multi-search-engine/0.2 (+https://github.com/opensquilla/opensquilla)"
TIMEOUT_S = 8.0
MAX_ATTEMPTS = 3
_RETRY_STATUS_CODES = {429, *range(500, 600)}
_RETRY_DELAYS_S = (0.25, 0.75)
_MAX_RETRY_DELAY_S = 5.0


@dataclass
class Result:
    engine: str
    title: str
    url: str
    snippet: str
    rank: int
    doi: str | None = None
    year: int | None = None
    authors: list[str] | None = None
    corporate_authors: list[str] | None = None


@dataclass
class EngineError:
    engine: str
    reason: str


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
        follow_redirects=True,
        timeout=httpx.Timeout(TIMEOUT_S, connect=4.0),
    )


def _retry_delay(response: object | None, attempt: int) -> float:
    """Return a small, bounded backoff while honoring short Retry-After values."""
    headers = getattr(response, "headers", {}) if response is not None else {}
    header_items = headers.items() if hasattr(headers, "items") else []
    normalized_headers = {str(key).lower(): value for key, value in header_items}
    retry_after = normalized_headers.get("retry-after")
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), _MAX_RETRY_DELAY_S)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(retry_after))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = (retry_at - datetime.now(UTC)).total_seconds()
                return min(max(delay, 0.0), _MAX_RETRY_DELAY_S)
            except (TypeError, ValueError, OverflowError):
                pass
    rate_interval = normalized_headers.get("x-rate-limit-interval")
    if rate_interval is not None:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*", str(rate_interval))
        if match:
            multiplier = {"ms": 0.001, "s": 1.0, "m": 60.0}.get(match.group(2) or "s", 1.0)
            return min(float(match.group(1)) * multiplier, _MAX_RETRY_DELAY_S)
    return _RETRY_DELAYS_S[min(attempt, len(_RETRY_DELAYS_S) - 1)]


def _request(client: Any, method: str, url: str, **kwargs: Any) -> Any:
    """Make one HTTP request with finite retries for timeout/429/5xx failures."""
    request = getattr(client, method.lower())
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = request(url, **kwargs)
        except httpx.TimeoutException:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(_retry_delay(None, attempt))
            continue

        status_code = int(getattr(response, "status_code", 200))
        if status_code in _RETRY_STATUS_CODES and attempt + 1 < MAX_ATTEMPTS:
            delay = _retry_delay(response, attempt)
            close = getattr(response, "close", None)
            if callable(close):
                close()
            time.sleep(delay)
            continue
        response.raise_for_status()
        return response
    raise RuntimeError("request retry loop exhausted")  # pragma: no cover


def _ddg_search(query: str, limit: int) -> list[Result]:
    with _client() as client:
        response = _request(
            client,
            "post",
            "https://html.duckduckgo.com/html/",
            data={"q": query},
        )
        soup = BeautifulSoup(response.text, "html.parser")
        results: list[Result] = []
        for idx, item in enumerate(soup.select("div.result")[:limit], start=1):
            title_el = item.select_one("a.result__a")
            snippet_el = item.select_one("a.result__snippet")
            if title_el is None:
                continue
            results.append(
                Result(
                    engine="duckduckgo",
                    title=title_el.get_text(strip=True),
                    url=str(title_el.get("href") or ""),
                    snippet=snippet_el.get_text(strip=True) if snippet_el is not None else "",
                    rank=idx,
                )
            )
        return results


_BRAVE_MAX_COUNT = 20  # Brave Web Search API hard-caps `count` at 20; >20 → HTTP 422.


def _brave_search(query: str, limit: int) -> list[Result]:
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY") or os.environ.get("BRAVE_API_KEY")
    if not api_key:
        raise RuntimeError("BRAVE_SEARCH_API_KEY/BRAVE_API_KEY not set; skipping")
    effective_count = min(max(limit, 1), _BRAVE_MAX_COUNT)
    if limit > _BRAVE_MAX_COUNT:
        print(
            f"[multi-search-engine] brave count clamped {limit}→{_BRAVE_MAX_COUNT} "
            f"(API hard-cap)",
            file=sys.stderr,
        )
    with _client() as client:
        response = _request(
            client,
            "get",
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": effective_count},
            headers={"X-Subscription-Token": api_key},
        )
        payload = response.json()
        items = payload.get("web", {}).get("results", []) or []
        results: list[Result] = []
        for idx, item in enumerate(items[:limit], start=1):
            results.append(
                Result(
                    engine="brave",
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", ""),
                    rank=idx,
                )
            )
        return results


_TAVILY_MAX_RESULTS = 20


def _tavily_search(query: str, limit: int) -> list[Result]:
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY not set; skipping")
    effective_count = min(max(limit, 1), _TAVILY_MAX_RESULTS)
    if limit > _TAVILY_MAX_RESULTS:
        print(
            f"[multi-search-engine] tavily max_results clamped "
            f"{limit}→{_TAVILY_MAX_RESULTS} (API hard-cap)",
            file=sys.stderr,
        )
    with _client() as client:
        response = _request(
            client,
            "post",
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": effective_count,
            },
        )
        payload = response.json()
        items = payload.get("results", []) or []
        results: list[Result] = []
        for idx, item in enumerate(items[:effective_count], start=1):
            results.append(
                Result(
                    engine="tavily",
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    rank=idx,
                )
            )
        return results


def _clean_doi(value: object) -> str | None:
    raw = unquote(str(value or "")).strip()
    raw = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^doi:\s*", "", raw, flags=re.IGNORECASE)
    match = re.search(r"10\.\d{4,9}/[^\s?#\"<>]+", raw, flags=re.IGNORECASE)
    if match is None:
        return None
    doi = match.group(0).rstrip(".,;")
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    return doi.lower() or None


def _crossref_query(query: str) -> str:
    """Remove web-engine site operators that are meaningless to Crossref."""
    cleaned = re.sub(r"\bsite:[^\s)]+", " ", query, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:AND|OR)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("(", " ").replace(")", " ")
    return " ".join(cleaned.split()) or query


def _crossref_year(item: dict[str, object]) -> int | None:
    for field in ("published-print", "published-online", "published", "posted", "issued"):
        date_value = item.get(field)
        if not isinstance(date_value, dict):
            continue
        date_parts = date_value.get("date-parts")
        if not isinstance(date_parts, list) or not date_parts:
            continue
        first = date_parts[0]
        if not isinstance(first, list) or not first:
            continue
        try:
            year = int(first[0])
        except (TypeError, ValueError):
            continue
        if 1000 <= year <= 9999:
            return year
    return None


def _crossref_authors(item: dict[str, object]) -> tuple[list[str] | None, list[str] | None]:
    raw_authors = item.get("author")
    if not isinstance(raw_authors, list):
        return None, None
    authors: list[str] = []
    corporate_authors: list[str] = []
    for raw_author in raw_authors:
        if not isinstance(raw_author, dict):
            continue
        literal = str(raw_author.get("literal") or raw_author.get("name") or "").strip()
        if literal:
            authors.append(literal)
            corporate_authors.append(literal)
            continue
        given = str(raw_author.get("given") or "").strip()
        family = str(raw_author.get("family") or "").strip()
        name = " ".join(part for part in (given, family) if part)
        if name:
            authors.append(name)
    return authors or None, corporate_authors or None


def _crossref_title(item: dict[str, object]) -> str:
    raw_title = item.get("title")
    if isinstance(raw_title, list):
        return str(raw_title[0]).strip() if raw_title else ""
    return str(raw_title or "").strip()


def _crossref_snippet(item: dict[str, object]) -> str:
    container = item.get("container-title")
    if isinstance(container, list):
        container_text = str(container[0]).strip() if container else ""
    else:
        container_text = str(container or "").strip()
    if container_text:
        return container_text
    abstract = str(item.get("abstract") or "").strip()
    if not abstract:
        return ""
    text = " ".join(BeautifulSoup(abstract, "html.parser").get_text(" ").split())
    return text[:500]


def _crossref_search(query: str, limit: int) -> list[Result]:
    effective_limit = max(0, limit)
    if effective_limit == 0:
        return []
    params: dict[str, object] = {
        "query.bibliographic": _crossref_query(query),
        "rows": effective_limit,
    }
    mailto = os.environ.get("CROSSREF_MAILTO", "").strip()
    if mailto:
        params["mailto"] = mailto
    with _client() as client:
        response = _request(
            client,
            "get",
            "https://api.crossref.org/v1/works",
            params=params,
            headers={"Accept": "application/json"},
        )
        payload = response.json()

    message = payload.get("message", {}) if isinstance(payload, dict) else {}
    items = message.get("items", []) if isinstance(message, dict) else []
    results: list[Result] = []
    for item in items[:effective_limit] if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        doi = _clean_doi(item.get("DOI"))
        if doi is None:
            continue
        authors, corporate_authors = _crossref_authors(item)
        results.append(
            Result(
                engine="crossref",
                title=_crossref_title(item),
                url=f"https://doi.org/{doi}",
                snippet=_crossref_snippet(item),
                rank=len(results) + 1,
                doi=doi,
                year=_crossref_year(item),
                authors=authors,
                corporate_authors=corporate_authors,
            )
        )
    return results


ENGINES: dict[str, Callable[[str, int], list[Result]]] = {
    "crossref": _crossref_search,
    "duckduckgo": _ddg_search,
    "brave": _brave_search,
    "tavily": _tavily_search,
}


_ARXIV_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(?P<id>(?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7}))(?:v\d+)?(?:\.pdf)?",
    re.IGNORECASE,
)


def _result_doi(result: Result) -> str | None:
    return _clean_doi(result.doi) or _clean_doi(result.url)


def _result_arxiv(result: Result) -> str | None:
    match = _ARXIV_RE.search(unquote(result.url))
    return match.group("id").lower() if match else None


def _normalized_url(url: str) -> str | None:
    try:
        split = urlsplit(url.strip())
    except ValueError:
        return None
    if not split.scheme or not split.netloc:
        return url.strip().rstrip("/") or None
    scheme = split.scheme.lower()
    if scheme in {"http", "https"}:
        scheme = "https"
    hostname = (split.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    try:
        port = split.port
    except ValueError:
        return None
    if port is not None and port not in {80, 443}:
        hostname = f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", unquote(split.path)).rstrip("/") or "/"
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(split.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
        )
    )
    return urlunsplit((scheme, hostname, path, query, ""))


def _identity_keys(result: Result) -> list[str]:
    keys: list[str] = []
    doi = _result_doi(result)
    if doi:
        keys.append(f"doi:{doi}")
    arxiv = _result_arxiv(result)
    if arxiv:
        keys.append(f"arxiv:{arxiv}")
    normalized_url = _normalized_url(result.url)
    if normalized_url:
        keys.append(f"url:{normalized_url}")
    return keys


def _result_payload(result: Result) -> dict[str, object]:
    """Keep the legacy result shape while emitting scholarly fields when known."""
    return {key: value for key, value in result.__dict__.items() if value is not None}


def _normalize_query(query: str) -> str:
    """Extract the actual web query from structured planner output."""
    lines = [line.strip() for line in query.splitlines() if line.strip()]
    for line in lines:
        if line.upper().startswith("SEARCH_QUERY:"):
            extracted = line.split(":", 1)[1].strip()
            if extracted:
                return extracted
    return query.strip()


def search_all(
    query: str,
    engines: list[str],
    limit: int,
    strict: bool,
) -> dict[str, object]:
    normalized_query = _normalize_query(query)
    results: list[Result] = []
    errors: list[EngineError] = []
    handlers: list[tuple[str, Callable[[str, int], list[Result]] | None, str | None]] = []
    for name in engines:
        handler = ENGINES.get(name)
        if handler is None:
            handlers.append((name, None, "unknown engine"))
            continue
        handlers.append((name, handler, None))

    def _run_engine(handler: Callable[[str, int], list[Result]]) -> list[Result]:
        return handler(normalized_query, limit)

    def _append_engine_results(engine_results: list[Result]) -> None:
        engine_results.sort(
            key=lambda result: (result.rank, result.title.casefold(), result.url)
        )
        results.extend(engine_results)

    if strict:
        for name, _handler, known_error in handlers:
            if known_error is not None:
                errors.append(EngineError(name, known_error))
                break
            assert _handler is not None
            try:
                engine_results = _run_engine(_handler)
            except Exception as exc:  # network, key missing, or parser failure
                errors.append(EngineError(name, str(exc)))
                break
            _append_engine_results(engine_results)
    else:
        max_workers = max(1, len(handlers))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                name: executor.submit(_run_engine, handler)
                for name, handler, known_error in handlers
                if handler is not None and known_error is None
            }

            for name, _handler, known_error in handlers:
                if known_error is not None:
                    errors.append(EngineError(name, known_error))
                    continue
                try:
                    engine_results = futures[name].result()
                except Exception as exc:  # network, key missing, parser breaks — keep going
                    errors.append(EngineError(name, str(exc)))
                    continue
                _append_engine_results(engine_results)

    deduplicated: list[dict[str, object]] = []
    seen: set[str] = set()
    for result in results:
        identities = _identity_keys(result)
        if any(identity in seen for identity in identities):
            continue
        seen.update(identities)
        deduplicated.append(_result_payload(result))
    return {
        "query": normalized_query,
        "results": deduplicated,
        "errors": [e.__dict__ for e in errors],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Multi-engine web search.")
    parser.add_argument("--query", required=True)
    parser.add_argument(
        "--engines",
        default="duckduckgo",
        help="Comma-separated engine list (crossref,duckduckgo,brave,tavily)",
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--strict", action="store_true", help="Fail on first engine error")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="(default; kept for clarity)")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    payload = search_all(args.query, engines, args.limit, args.strict)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 1 if args.strict and payload["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
