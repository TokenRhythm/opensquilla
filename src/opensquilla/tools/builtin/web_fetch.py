"""web_fetch built-in tool: fetch a URL and extract readable content."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from cachetools import TTLCache

from opensquilla.result_budget import (
    DEFAULT_TOOL_RUN_BUDGET_POLICY,
    ToolRunBudgetPolicy,
)
from opensquilla.sandbox.integration import managed_network_httpx_kwargs
from opensquilla.sandbox.operation_runtime import (
    NetworkOperationRequest,
    SandboxToolDescriptor,
)
from opensquilla.tools.registry import tool
from opensquilla.tools.ssrf import environment_proxy_url as _environment_proxy_url
from opensquilla.tools.ssrf import pinned_transport as _pinned_transport
from opensquilla.tools.ssrf import validate_http_url_for_fetch
from opensquilla.tools.types import PlanAccess, SSRFBlockedError, current_tool_context

log = structlog.get_logger(__name__)

# 15-minute cache keyed by (url, extract_mode, extractor preference)
_cache: TTLCache = TTLCache(maxsize=256, ttl=900)

# Escalate to Firecrawl when local readability returns None or content below
# this threshold. Keeps free local path as the default, reserves the paid SaaS
# path for JS-heavy / anti-bot pages where readability struggles.
_READABILITY_ESCALATION_MIN_CHARS = 200

_DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

_UA_PRIMARY = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_UA_FALLBACK = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)

_TRANSIENT_STATUSES: frozenset[int] = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
_RETRY_DELAY_SECONDS = 0.25
_WEB_FETCH_DEFAULT_MAX_CHARS = 20_000
_WEB_FETCH_MAX_CHARS_ENV = "OPENSQUILLA_WEB_FETCH_MAX_CHARS"
_MAX_REDIRECTS = 5

_XML_ATTR_ESCAPES = {
    "<": "&lt;",
    ">": "&gt;",
    "&": "&amp;",
    '"': "&quot;",
    "'": "&apos;",
}

_RAW_TOOL_RESULT_KEY = "_raw_tool_result"


def _web_fetch_request(args: Mapping[str, Any]) -> NetworkOperationRequest:
    url = str(args.get("url", "") or "")
    parsed = urlparse(url)
    return NetworkOperationRequest(
        url=url,
        method="GET",
        host=parsed.hostname or "",
    )


def _check_ssrf(url: str) -> list[str]:
    """Validate the URL and return the vetted IPs to pin the connection to.

    Raises ValueError/SSRFBlockedError if the URL resolves to a private or
    internal address.
    """
    return validate_http_url_for_fetch(url)


def _html_to_markdown(html: str) -> str:
    import html2text

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    h.body_width = 0
    return h.handle(html)


def _markdown_to_text(markdown: str) -> str:
    """Strip markdown formatting to plain text via html2text."""
    import html2text

    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    h.body_width = 0
    # html2text can also strip simple markdown when fed as plain text
    # but the cleanest approach: pass through as-is since we already
    # have the markdown. Just strip link/image noise.
    return h.handle(markdown)


async def _try_firecrawl(url: str, api_key: str) -> tuple[str, str, str] | None:
    """Try Firecrawl API. Returns (content, extractor, title) or None."""
    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            **managed_network_httpx_kwargs(),
        ) as client:
            resp = await client.post(
                "https://api.firecrawl.dev/v2/scrape",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "url": url,
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                    "maxAge": 900_000,
                },
            )
            data = resp.json()
            if data.get("success"):
                scraped = data.get("data") or {}
                if not isinstance(scraped, dict):
                    return None
                metadata = scraped.get("metadata") or {}
                title = metadata.get("title") if isinstance(metadata, dict) else ""
                return str(scraped.get("markdown") or ""), "firecrawl", str(title or "")
            log.warning("web_fetch.firecrawl_unsuccessful", url=url, response=data)
    except Exception as exc:
        log.warning("web_fetch.firecrawl_error", url=url, error=str(exc))
    return None


def _try_readability(html: str) -> tuple[str, str, str] | None:
    """Try readability-lxml. Returns (title, content_markdown, extractor) or None."""
    try:
        from readability import Document

        doc = Document(html)
        title = doc.title()
        summary_html = doc.summary()
        content = _html_to_markdown(summary_html)
        return title, content, "readability"
    except Exception:
        return None


def _try_html2text(html: str) -> tuple[str, str, str]:
    """html2text fallback — always succeeds."""
    content = _html_to_markdown(html)
    return "", content, "html2text"


def _resolve_default_max_chars() -> int:
    """Return default output cap for omitted max_chars."""
    raw = os.environ.get(_WEB_FETCH_MAX_CHARS_ENV, "").strip()
    if not raw:
        return _WEB_FETCH_DEFAULT_MAX_CHARS
    try:
        value = int(raw)
    except ValueError:
        return _WEB_FETCH_DEFAULT_MAX_CHARS
    return value if value >= 100 else _WEB_FETCH_DEFAULT_MAX_CHARS


def _resolve_effective_max_chars(max_chars: int | None) -> int | None:
    """Resolve explicit max_chars or the default cap for omitted values."""
    max_allowed = _active_run_budget_policy().max_single_fetch_chars
    if max_chars is not None:
        if max_chars < 100:
            return None
        return min(max_chars, max_allowed) if max_allowed is not None else max_chars
    default = _resolve_default_max_chars()
    return min(default, max_allowed) if max_allowed is not None else default


def _active_run_budget_policy() -> ToolRunBudgetPolicy:
    ctx = current_tool_context.get()
    policy = getattr(ctx, "tool_run_budget_policy", None) if ctx is not None else None
    if isinstance(policy, ToolRunBudgetPolicy):
        return policy
    return DEFAULT_TOOL_RUN_BUDGET_POLICY


async def run_web_fetch_payload(
    url: str,
    extract_mode: str = "markdown",
    max_chars: int | None = None,
    extractor: str = "auto",
) -> dict[str, Any]:
    # --- SSRF guard ---
    _check_ssrf(url)
    from opensquilla.tools.builtin.web import _sensitive_body_block, _sensitive_url_marker

    marker = _sensitive_url_marker(url)
    if marker is not None:
        return {_RAW_TOOL_RESULT_KEY: _sensitive_body_block("web_fetch", marker)}

    effective_max_chars = _resolve_effective_max_chars(max_chars)

    # --- Cache lookup ---
    extractor_preference = extractor or "auto"
    cache_key = (url, extract_mode, extractor_preference)
    if cache_key in _cache:
        cached: dict[str, Any] = dict(_cache[cache_key])
        return _apply_max_chars(cached, effective_max_chars)

    if extractor_preference == "firecrawl":
        firecrawl_key = os.environ.get("FIRECRAWL_API_KEY", "")
        if not firecrawl_key:
            return {
                "url": url,
                "final_url": url,
                "status": 0,
                "content_type": "",
                "title": "",
                "extract_mode": extract_mode,
                "extractor": "firecrawl",
                "truncated": False,
                "length": 0,
                "text": "",
                "error": "FIRECRAWL_API_KEY is required for explicit Firecrawl extraction.",
            }
        fc_result = await _try_firecrawl(url, firecrawl_key)
        if fc_result is None:
            return {
                "url": url,
                "final_url": url,
                "status": 0,
                "content_type": "",
                "title": "",
                "extract_mode": extract_mode,
                "extractor": "firecrawl",
                "truncated": False,
                "length": 0,
                "text": "",
                "error": "Firecrawl scrape did not return content.",
            }
        extracted_content, extractor_used, title = fc_result
        if extract_mode == "text":
            extracted_content = _markdown_to_text(extracted_content)
        firecrawl_payload = {
            "url": url,
            "final_url": url,
            "status": 200,
            "content_type": "text/markdown",
            "title": title,
            "extract_mode": extract_mode,
            "extractor": extractor_used,
            "truncated": False,
            "length": len(extracted_content),
            "text": _wrap_content(url, extracted_content),
        }
        _cache[cache_key] = firecrawl_payload
        return _apply_max_chars(firecrawl_payload, effective_max_chars)

    # --- Fetch ---
    title = ""
    content_type = ""
    final_url = url
    status = 0
    raw_html = ""

    async def _do_fetch(user_agent: str) -> tuple[int, str, str, str]:
        headers = dict(_DEFAULT_HEADERS)
        headers["User-Agent"] = user_agent
        managed_kwargs = managed_network_httpx_kwargs()
        current_url = url
        for _redirect_count in range(_MAX_REDIRECTS + 1):
            vetted = _check_ssrf(current_url)
            marker = _sensitive_url_marker(current_url)
            if marker is not None:
                raise ValueError("Blocked redirect URL containing sensitive data")

            # Pin the connection to the address that just passed the SSRF guard
            # so a rebinding second DNS resolution cannot reach a private IP.
            # When a managed proxy is active it already resolves once through the
            # guarded path, so skip client-side pinning in that mode.
            transport = None
            if "proxy" not in managed_kwargs:
                transport_kwargs: dict[str, object] = {}
                if managed_kwargs.get("trust_env"):
                    proxy_url = _environment_proxy_url(current_url)
                    if proxy_url is not None:
                        transport_kwargs["proxy"] = proxy_url
                transport = _pinned_transport(current_url, vetted, **transport_kwargs)
            client_kwargs: dict[str, object] = {
                "timeout": 30.0,
                "follow_redirects": False,
                "headers": headers,
                **managed_kwargs,
            }
            if transport is not None:
                client_kwargs["transport"] = transport
            async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
                response = await client.get(current_url)
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("location")
            if not location:
                break
            current_url = urljoin(current_url, location)
        else:
            raise ValueError(f"Too many redirects (>{_MAX_REDIRECTS})")

        return (
            response.status_code,
            current_url,
            response.headers.get("content-type", ""),
            response.text,
        )

    last_error: str | None = None
    for attempt_idx, user_agent in enumerate((_UA_PRIMARY, _UA_FALLBACK)):
        try:
            status, final_url, content_type, raw_html = await _do_fetch(user_agent)
        except SSRFBlockedError:
            raise
        except httpx.TimeoutException:
            return {
                "url": url,
                "final_url": url,
                "status": 0,
                "content_type": "",
                "title": "",
                "extract_mode": extract_mode,
                "extractor": "none",
                "truncated": False,
                "length": 0,
                "text": "",
                "error": "timed_out",
                "hint": (
                    "The source timed out while loading; skip it, retry later, "
                    "or use another source."
                ),
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt_idx == 0:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            result: dict[str, Any] = {
                "url": url,
                "final_url": url,
                "status": 0,
                "content_type": "",
                "title": "",
                "extract_mode": extract_mode,
                "extractor": "none",
                "truncated": False,
                "length": 0,
                "text": "",
                "error": last_error,
            }
            return result

        is_transient = status in _TRANSIENT_STATUSES
        is_empty_success = 200 <= status < 300 and not raw_html.strip()
        if attempt_idx == 0 and (is_transient or is_empty_success):
            await asyncio.sleep(_RETRY_DELAY_SECONDS)
            continue
        break

    # --- Non-HTML: return as-is ---
    is_html = "html" in content_type.lower()
    if not is_html:
        result = {
            "url": url,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "title": "",
            "extract_mode": extract_mode,
            "extractor": "raw",
            "truncated": False,
            "length": len(raw_html),
            "text": _wrap_content(final_url, raw_html),
        }
        _cache[cache_key] = result
        return _apply_max_chars(result, effective_max_chars)

    # --- Error HTTP status: return empty ---
    if status >= 400:
        hint = (
            "rate-limited or blocked upstream; try a different URL from search results, "
            "retry after a brief delay, or use another source"
            if status in _TRANSIENT_STATUSES
            else "HTTP error from upstream; try a different URL or adjust the path"
        )
        result = {
            "url": url,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "title": "",
            "extract_mode": extract_mode,
            "extractor": "none",
            "truncated": False,
            "length": 0,
            "text": "",
            "error": hint,
        }
        if status not in _TRANSIENT_STATUSES:
            _cache[cache_key] = result
        return result

    # --- Extraction pipeline ---
    # Try local extractors first (zero-cost, handles ~90% of mainstream pages),
    # escalate to Firecrawl only when readability misses (JS-heavy / anti-bot
    # sites), and fall back to html2text for everything else.
    extracted_content = ""
    extractor_used = "html2text"

    # 1. readability-lxml (local, free, main-content extraction)
    rd_result = _try_readability(raw_html)
    if rd_result is not None:
        title, extracted_content, extractor_used = rd_result

    # 2. Firecrawl escalation — only when readability returns nothing or too
    # little content (SaaS call, requires API key)
    readability_short = len(extracted_content) < _READABILITY_ESCALATION_MIN_CHARS
    firecrawl_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if firecrawl_key and readability_short:
        log.info(
            "web_fetch.firecrawl_escalation",
            url=url,
            readability_chars=len(extracted_content),
            reason="readability_miss" if rd_result is None else "readability_short",
        )
        fc_result = await _try_firecrawl(url, firecrawl_key)
        if fc_result is not None:
            extracted_content, extractor_used, firecrawl_title = fc_result
            title = title or firecrawl_title

    # 3. html2text fallback — always succeeds on valid HTML
    if not extracted_content:
        title, extracted_content, extractor_used = _try_html2text(raw_html)

    # --- Mode conversion ---
    if extract_mode == "text":
        extracted_content = _markdown_to_text(extracted_content)

    result = {
        "url": url,
        "final_url": final_url,
        "status": status,
        "content_type": content_type,
        "title": title,
        "extract_mode": extract_mode,
        "extractor": extractor_used,
        "truncated": False,
        "length": len(extracted_content),
        "text": _wrap_content(final_url, extracted_content),
    }
    _cache[cache_key] = result
    return _apply_max_chars(result, effective_max_chars)


@tool(
    name="web_fetch",
    description=(
        "Fetch a URL and extract readable content as markdown or plain text. "
        "Uses a multi-extractor pipeline (readability → Firecrawl escalation → "
        "html2text). Includes SSRF protection and a 15-minute response cache."
    ),
    params={
        "url": {
            "type": "string",
            "description": "HTTP or HTTPS URL to fetch.",
        },
        "extract_mode": {
            "type": "string",
            "description": 'Extraction format: "markdown" (default) or "text".',
            "enum": ["markdown", "text"],
        },
        "max_chars": {
            "type": "integer",
            "description": (
                "Maximum characters to return (minimum 100). "
                "Defaults to 20,000 when omitted; override default with "
                "OPENSQUILLA_WEB_FETCH_MAX_CHARS."
            ),
            "minimum": 100,
        },
        "extractor": {
            "type": "string",
            "description": 'Extractor preference: "auto" (default) or "firecrawl".',
            "enum": ["auto", "firecrawl"],
        },
    },
    required=["url"],
    plan_access=PlanAccess.READ_ONLY,
    result_budget_class="external",
    sandbox=SandboxToolDescriptor.network(
        kind="web.fetch",
        argv_factory=lambda a: (
            "web_fetch",
            str(a.get("url", "")),
            str(a.get("extract_mode", "markdown")),
            str(a.get("extractor", "auto")),
        ),
        request_factory=_web_fetch_request,
        record_payload=False,
    ),
)
async def web_fetch(
    url: str,
    extract_mode: str = "markdown",
    max_chars: int | None = None,
    extractor: str = "auto",
) -> str:
    payload = await run_web_fetch_payload(
        url,
        extract_mode=extract_mode,
        max_chars=max_chars,
        extractor=extractor,
    )
    raw_tool_result = payload.get(_RAW_TOOL_RESULT_KEY)
    if isinstance(raw_tool_result, str):
        return raw_tool_result
    return json.dumps(payload, ensure_ascii=False)


def _wrap_content(source: str, content: str) -> str:
    safe_source = _xml_escape_attr(source)
    safe_content = _escape_external_content_boundaries(content)
    return f'<external-content source="{safe_source}">{safe_content}</external-content>'


def _xml_escape_attr(value: str) -> str:
    return "".join(_XML_ATTR_ESCAPES.get(ch, ch) for ch in value)


def _escape_external_content_boundaries(value: str) -> str:
    out = re.sub(
        r"<\s*/\s*external-content\s*>",
        "&lt;/external-content&gt;",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"<\s*external-content\b",
        "&lt;external-content",
        out,
        flags=re.IGNORECASE,
    )


def _extract_inner(wrapped: str) -> str:
    """Extract content from inside <external-content> tags."""
    start_tag_end = wrapped.find(">")
    end_tag_start = wrapped.rfind("</external-content>")
    if start_tag_end == -1 or end_tag_start == -1:
        return wrapped
    return wrapped[start_tag_end + 1 : end_tag_start]


def _apply_max_chars(result: dict[str, Any], max_chars: int | None) -> dict[str, Any]:
    """Return a display copy with max_chars applied.

    The cache stores untruncated content so callers can later request a larger
    explicit cap without waiting for cache expiry.
    """
    if max_chars is None:
        return dict(result)

    output = dict(result)
    inner = _extract_inner(str(output.get("text", "")))
    if len(inner) <= max_chars:
        output["original_length"] = len(inner)
        output["returned_length"] = len(inner)
        return output

    source = str(output.get("final_url") or output.get("url") or "")
    output["text"] = _wrap_content(source, inner[:max_chars])
    output["truncated"] = True
    output["original_length"] = len(inner)
    output["returned_length"] = max_chars
    output["length"] = len(inner)
    return output
