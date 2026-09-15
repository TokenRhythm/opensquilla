"""Deterministic model-only previews of canonical search results."""

from __future__ import annotations

import json

from opensquilla.engine.tokenjuice_adapter import TokenjuiceReduction

SEARCH_PREVIEW_REDUCER = "builtin_web_search"


def reduce_canonical_search(content: str) -> TokenjuiceReduction | None:
    """Recognize the built-in success contract; leave other payloads intact."""
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    if set(payload) != {
        "ok",
        "query",
        "mode",
        "provider_attempts",
        "diagnostics",
        "sources",
        "results",
    }:
        return None
    if (
        not isinstance(payload["query"], str)
        or not isinstance(payload["mode"], str)
        or not isinstance(payload["provider_attempts"], list)
        or not isinstance(payload["diagnostics"], dict)
        or not isinstance(payload["sources"], list)
        or not isinstance(payload["results"], list)
    ):
        return None
    previews = []
    for hit in payload["results"]:
        if not isinstance(hit, dict) or any(
            not isinstance(hit.get(key), str)
            for key in (
                "title",
                "url",
                "canonical_url",
                "provider",
                "fetch_status",
                "excerpt",
                "snippet",
            )
        ):
            return None
        if "error" in hit or not isinstance(hit.get("fetched"), bool):
            return None
        highlights = hit.get("highlights")
        if not isinstance(highlights, list) or any(not isinstance(x, str) for x in highlights):
            return None
        excerpt = next((x for x in (hit["excerpt"], hit["snippet"], *highlights) if x.strip()), "")
        preview = {
            key: hit.get(key)
            for key in (
                "rank",
                "title",
                "url",
                "provider",
                "published_at",
                "fetched",
                "fetch_status",
                "content_truncated",
            )
        }
        preview["excerpt"] = excerpt
        # Highlights can contain query-specific evidence absent from the page
        # prefix. Drop only blank text or literal coverage by retained content;
        # similar wording is not evidence that two passages are interchangeable.
        retained_highlights: list[str] = []
        for highlight in highlights:
            candidate = highlight.strip()
            if candidate and not any(
                candidate in retained for retained in (excerpt, *retained_highlights)
            ):
                retained_highlights.append(highlight)
        if retained_highlights:
            preview["highlights"] = retained_highlights
        previews.append(preview)
    # Failure diagnostics (including failed fallback attempts) remain recoverable
    # and visible. The primary excerpt replaces alternate summaries, while
    # complementary highlights remain visible alongside it.
    reduced = {key: value for key, value in payload.items() if key not in {"sources", "results"}}
    reduced["results"] = previews
    inline = json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))
    return TokenjuiceReduction(
        inline_text=inline,
        raw_chars=len(content),
        reduced_chars=len(inline),
        ratio=len(inline) / max(1, len(content)),
        reducer=SEARCH_PREVIEW_REDUCER,
    )
