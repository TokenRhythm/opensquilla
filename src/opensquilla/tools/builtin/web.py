"""Web built-in tools: http_request, web_search, web_discover."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlparse

import httpx

from opensquilla.sandbox.integration import (
    current_managed_network_proxy_url,
    managed_network_httpx_kwargs,
)
from opensquilla.sandbox.operation_runtime import (
    NetworkOperationRequest,
    SandboxToolDescriptor,
)
from opensquilla.search.canonical import run_canonical_web_search
from opensquilla.search.normalize import canonicalize_url, extract_domain
from opensquilla.search.types import (
    DEFAULT_SEARCH_MAX_RESULTS,
    MAX_SEARCH_RESULTS,
    Recency,
    SearchMode,
    SearchOptions,
    SearchProviderError,
    SearchResult,
)
from opensquilla.tools.path_policy import reject_foreign_host_path
from opensquilla.tools.registry import tool
from opensquilla.tools.run_mode import full_host_access_active
from opensquilla.tools.types import ToolError, UnsupportedURLSchemeError, current_tool_context


def _validate_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsupportedURLSchemeError(url)


_SECRET_KEY_PATTERN = (
    r"API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE[_-]?KEY|"
    r"ACCESS[_-]?KEY|AUTHORIZATION|BEARER"
)
_SECRET_NAME_RE = re.compile(_SECRET_KEY_PATTERN, re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)(?:^|[\s\"'{,])(?:\d+\t)?"
    rf"[A-Z0-9_]*(?:{_SECRET_KEY_PATTERN})[A-Z0-9_]*\s*[:=]"
)
_SECRET_JSON_KEY_RE = re.compile(
    rf"(?im)(?:^|[\s{{,])['\"][^'\"\n]{{0,80}}(?:{_SECRET_KEY_PATTERN})"
    r"[^'\"\n]{0,80}['\"]\s*:"
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_PASSWD_ENTRY_RE = re.compile(r"(?m)^(?:\d+\t)?[a-z_][a-z0-9_-]*:x?:\d+:\d+:")
_SENSITIVE_HTTP_METHODS = {"POST", "PUT", "PATCH"}
_TEXT_BODY_LIMIT = 10_000
_BINARY_BODY_LIMIT = 1_000_000
_FETCH_DIR_NAME = ".fetch"
_SEARCH_BENCHMARK_BLOCKLIST_ENV = "OPENSQUILLA_SEARCH_BENCHMARK_BLOCKLIST"
_SEARCH_BENCHMARK_BLOCKLIST_TERMS_ENV = "OPENSQUILLA_SEARCH_BENCHMARK_BLOCKLIST_TERMS"
_SEARCH_BENCHMARK_BLOCKLIST_DEFAULT_TERMS = (
    "huggingface.co/datasets/perplexity-ai/draco",
    "hf.co/datasets/perplexity-ai/draco",
    "datasets/perplexity-ai/draco",
    "perplexity-ai/draco",
    "perplexity-ai draco",
    "draco benchmark",
    "draco dataset",
    "draco rubric",
    "draco ground truth",
    "draco mini_test",
    "draco test.jsonl",
)
_VALID_SEARCH_MODES: frozenset[str] = frozenset({"auto", "news", "technical", "broad"})
_VALID_SEARCH_RECENCIES: frozenset[str] = frozenset({"day", "week", "month", "year"})
_VALID_SEARCH_PROVIDERS: frozenset[str] = frozenset(
    {"auto", "bocha", "tavily", "brave", "duckduckgo", "exa", "iqs"}
)

def _network_http_request(args: Mapping[str, Any]) -> NetworkOperationRequest:
    url = str(args.get("url", "") or "")
    parsed = urlparse(url)
    raw_headers = args.get("headers")
    headers = (
        {str(key): str(value) for key, value in raw_headers.items()}
        if isinstance(raw_headers, Mapping)
        else {}
    )
    return NetworkOperationRequest(
        url=url,
        method=str(args.get("method", "GET") or "GET").upper(),
        host=parsed.hostname or "",
        headers=headers,
        body=str(args.get("body")) if args.get("body") is not None else None,
        output_path=Path(str(args["output_path"]))
        if args.get("output_path") is not None
        else None,
    )


def _network_search_request(args: Mapping[str, Any]) -> NetworkOperationRequest:
    return NetworkOperationRequest(
        method="SEARCH",
        host="",
        body=str(args.get("query", "") or ""),
    )


def _web_search_sandbox_argv(args: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        "web_search",
        str(args.get("query", "")),
        str(args.get("fetch_top_k", "")),
        _search_plan_argv_token(args, tool_name="web_search"),
    )


def _web_discover_sandbox_argv(args: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        "web_discover",
        str(args.get("query", "")),
        str(args.get("max_results", "")),
        _search_plan_argv_token(args, tool_name="web_discover"),
    )


def _search_plan_argv_token(args: Mapping[str, Any], *, tool_name: str) -> str:
    """Encode only planned provider ids for managed-network domain grants."""

    try:
        from opensquilla.search.runtime_config import get_resolved_search_runtime

        provider_value = args.get("provider")
        provider = (
            str(provider_value)
            if isinstance(provider_value, str) and provider_value not in {"", "auto"}
            else None
        )
        if (
            tool_name == "web_discover"
            and provider is None
            and _active_provider not in _VALID_SEARCH_PROVIDERS
        ):
            provider = _active_provider
        raw_mode = str(args.get("mode") or "auto")
        mode = raw_mode if raw_mode in _VALID_SEARCH_MODES else "auto"
        raw_recency = args.get("recency")
        recency = (
            str(raw_recency)
            if isinstance(raw_recency, str) and raw_recency in _VALID_SEARCH_RECENCIES
            else None
        )
        options = SearchOptions(
            query=str(args.get("query") or ""),
            mode=cast(SearchMode, mode),
            include_domains=_search_plan_domains(args.get("include_domains")),
            exclude_domains=_search_plan_domains(args.get("exclude_domains")),
            recency=cast(Recency | None, recency),
            provider=provider,
        )
        plan = get_resolved_search_runtime().execution_plan(options)
        provider_names = plan.provider_names
    except Exception:  # pragma: no cover - fail closed to the legacy known-provider grant
        provider_names = (_active_provider,)
        if _active_search_fallback_policy == "network" and _active_provider != "duckduckgo":
            provider_names += ("duckduckgo",)
    return "providers=" + ",".join(provider_names)


def _search_plan_domains(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def _search_benchmark_blocklist_enabled() -> bool:
    value = os.environ.get(_SEARCH_BENCHMARK_BLOCKLIST_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _search_benchmark_blocklist_terms() -> list[str]:
    if not _search_benchmark_blocklist_enabled():
        return []
    terms = list(_SEARCH_BENCHMARK_BLOCKLIST_DEFAULT_TERMS)
    raw = os.environ.get(_SEARCH_BENCHMARK_BLOCKLIST_TERMS_ENV, "")
    for chunk in re.split(r"[\n,]", raw):
        term = chunk.strip().strip('"').strip("'")
        if len(term) >= 3:
            terms.append(term)
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = term.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(term)
    return deduped


def _search_benchmark_blocklist_matches(text: str, terms: list[str]) -> list[str]:
    normalized = text.lower()
    return [term for term in terms if term.lower() in normalized]


def _search_benchmark_result_text(result: SearchResult) -> str:
    return "\n".join([result.title or "", result.url or "", result.snippet or ""])


def _search_benchmark_blocked_query_payload(
    query: str,
    provider_name: str,
    *,
    attempts: list[dict[str, str]] | None = None,
) -> dict:
    payload = _search_payload(query, provider_name, [], attempts=attempts)
    payload["benchmark_blocklist_enabled"] = True
    payload["blocked_query"] = True
    payload["blocked_count"] = 0
    return payload


def _filter_search_benchmark_results(
    results: list[SearchResult],
    *,
    terms: list[str],
) -> tuple[list[SearchResult], dict[str, Any]]:
    if not terms:
        return results, {}
    filtered: list[SearchResult] = []
    blocked_count = 0
    for result in results:
        if _search_benchmark_blocklist_matches(_search_benchmark_result_text(result), terms):
            blocked_count += 1
            continue
        filtered.append(result)
    if blocked_count == 0:
        return filtered, {"benchmark_blocklist_enabled": True, "blocked_count": 0}
    return filtered, {
        "benchmark_blocklist_enabled": True,
        "blocked_count": blocked_count,
    }


class _BenchmarkBlocklistSearchProvider:
    def __init__(
        self,
        wrapped: Any,
        *,
        terms: list[str],
        meta: dict[str, Any],
    ) -> None:
        self._wrapped = wrapped
        self._terms = terms
        self._meta = meta

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    async def search(self, *args: Any, **kwargs: Any) -> list[SearchResult]:
        results = await self._wrapped.search(*args, **kwargs)
        filtered, blocklist_meta = _filter_search_benchmark_results(
            results,
            terms=self._terms,
        )
        if blocklist_meta:
            self._meta["benchmark_blocklist_enabled"] = True
            self._meta["blocked_count"] = int(self._meta.get("blocked_count") or 0) + int(
                blocklist_meta.get("blocked_count") or 0
            )
        return filtered


def _sensitive_body_marker(body: str | None) -> str | None:
    if full_host_access_active():
        return None
    if not body:
        return None
    if _PEM_PRIVATE_KEY_RE.search(body):
        return "private_key"
    if _PASSWD_ENTRY_RE.search(body):
        return "passwd_entry"
    if _SECRET_ASSIGNMENT_RE.search(body):
        return "secret_assignment"
    if _SECRET_JSON_KEY_RE.search(body):
        return "secret_json_key"
    return None


def _sensitive_url_marker(url: str) -> str | None:
    parsed = urlparse(url)
    for segment in parsed.path.split("/"):
        if _sensitive_body_marker(segment) is not None:
            return "sensitive_url_path"
    if not parsed.query:
        return None
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if _sensitive_body_marker(f"{key}={value}") is not None:
            return "sensitive_query"
    return None


def _sensitive_headers_marker(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    for key, value in headers.items():
        normalized_key = key.strip()
        if _SECRET_NAME_RE.search(normalized_key):
            return "sensitive_header"
        if _sensitive_body_marker(f"{normalized_key}={value}") is not None:
            return "sensitive_header"
        if normalized_key.lower() in {"authorization", "cookie", "proxy-authorization"}:
            return "sensitive_header"
    return None


def _sensitive_body_block(tool_name: str, marker: str) -> str:
    payload = {
        "status": "blocked",
        "reason": "sensitive_payload",
        "tool": tool_name,
        "sensitive_payload": marker,
        "message": (
            "Refusing to send an HTTP request body that appears to contain "
            "secrets or host account data. Remove the sensitive content or use "
            "an explicit operator-approved transfer path."
        ),
        "retryable": False,
    }
    return json.dumps(payload, ensure_ascii=False)


def _is_text_response_content_type(content_type: str) -> bool:
    normalized = content_type.lower().split(";", 1)[0].strip()
    if normalized.startswith("text/"):
        return True
    return (
        normalized in {"application/json", "application/xml", "application/xhtml+xml"}
        or normalized.endswith("+json")
        or normalized.endswith("+xml")
        or "json" in normalized
        or "xml" in normalized
    )


def _fetch_workspace_dir() -> Path:
    ctx = current_tool_context.get()
    if ctx is not None and ctx.workspace_dir:
        return Path(ctx.workspace_dir).expanduser().resolve()
    return Path.cwd().resolve()


def _fetch_root() -> Path:
    return (_fetch_workspace_dir() / _FETCH_DIR_NAME).resolve()


def _resolve_fetch_output_path(digest: str, output_path: str | None) -> Path:
    if output_path is None:
        root = _fetch_root()
        return root / f"{digest}.bin"

    raw = output_path.strip()
    if not raw:
        raise ToolError("output_path must not be empty")

    reject_foreign_host_path(raw, platform=os.name)
    root = _fetch_root()
    requested = Path(raw).expanduser()
    if requested.drive and not requested.is_absolute():
        raise ToolError("output_path must be an absolute path or a relative .fetch path")
    candidate = requested if requested.is_absolute() else root / requested
    resolved = candidate.resolve(strict=False)
    if resolved == root or not resolved.is_relative_to(root):
        raise ToolError(f"output_path must stay inside {root}")
    if resolved.exists() and resolved.is_dir():
        raise ToolError("output_path must name a file, not a directory")
    return resolved


def _save_http_response_body(raw_body: bytes, output_path: str | None) -> tuple[Path, str]:
    digest = hashlib.sha256(raw_body).hexdigest()
    path = _resolve_fetch_output_path(digest, output_path)
    if output_path is not None and path.exists():
        raise ToolError("output_path already exists")
    if output_path is None and path.exists():
        return path, digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw_body)
    return path, digest


@tool(
    name="http_request",
    description=(
        "Make an HTTP request. Use output_path to save a response under the workspace "
        ".fetch directory; otherwise responses are returned as bounded metadata."
    ),
    params={
        "url": {"type": "string", "description": "HTTP or HTTPS URL."},
        "method": {"type": "string", "description": "HTTP method (default: GET)."},
        "headers": {
            "type": "object",
            "description": "Request headers.",
            "additionalProperties": {"type": "string"},
        },
        "body": {"type": "string", "description": "Request body (for POST/PUT/PATCH)."},
        "timeout": {"type": "number", "description": "Request timeout in seconds (default 30)."},
        "output_path": {
            "type": "string",
            "description": "Optional file name/path inside the workspace .fetch directory.",
        },
    },
    required=["url"],
    owner_only=True,
    result_budget_class="external",
    sandbox=SandboxToolDescriptor.network(
        kind="network.http",
        argv_factory=lambda a: (
            "http_request",
            str(a.get("method", "GET")).upper(),
            str(a.get("url", "")),
            str(a.get("output_path", "")),
        ),
        request_factory=_network_http_request,
        record_payload=False,
    ),
)
async def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: float = 30.0,
    output_path: str | None = None,
) -> str:
    _validate_http_url(url)
    marker = _sensitive_url_marker(url)
    if marker is not None:
        return _sensitive_body_block("http_request", marker)
    marker = _sensitive_headers_marker(headers)
    if marker is not None:
        return _sensitive_body_block("http_request", marker)
    method_upper = method.upper()
    if method_upper in _SENSITIVE_HTTP_METHODS:
        marker = _sensitive_body_marker(body)
        if marker is not None:
            return _sensitive_body_block("http_request", marker)

    try:
        import httpx
    except ImportError:
        return "[error] httpx not installed. Run: pip install httpx"

    content: bytes | None = body.encode() if body else None

    async with httpx.AsyncClient(
        timeout=timeout,
        **managed_network_httpx_kwargs(),
    ) as client:
        response = await client.request(
            method=method_upper,
            url=url,
            headers=headers or {},
            content=content,
        )

    content_type = response.headers.get("content-type", "")
    is_text = _is_text_response_content_type(content_type)
    raw_body = response.content
    should_save = output_path is not None
    if should_save:
        saved_path, digest = _save_http_response_body(raw_body, output_path)
        preview = response.text[:_TEXT_BODY_LIMIT] if is_text else None
        result = {
            "status": response.status_code,
            "url": str(response.url),
            "headers": dict(response.headers),
            "content_type": content_type,
            "body": None,
            "body_base64": None,
            "body_truncated": False,
            "body_base64_truncated": False,
            "body_saved": True,
            "body_omitted_reason": "saved_to_file",
            "body_preview": preview,
            "path": str(saved_path),
            "size": len(raw_body),
            "sha256": digest,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    capped = raw_body[:_BINARY_BODY_LIMIT]
    body_base64 = base64.b64encode(capped).decode("ascii")
    body_base64_truncated = len(raw_body) > _BINARY_BODY_LIMIT
    if is_text:
        text_body = response.text
        body = text_body[:_TEXT_BODY_LIMIT]
        body_truncated = len(text_body) > _TEXT_BODY_LIMIT
    else:
        body = None
        body_truncated = False

    result = {
        "status": response.status_code,
        "url": str(response.url),
        "headers": dict(response.headers),
        "content_type": content_type,
        "body": body,
        "body_base64": body_base64,
        "body_truncated": body_truncated,
        "body_base64_truncated": body_base64_truncated,
        "body_saved": False,
        "path": None,
        "size": len(raw_body),
        "sha256": hashlib.sha256(raw_body).hexdigest(),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


# Active search provider name — set during boot
_active_provider: str = "duckduckgo"
_active_max_results: int = DEFAULT_SEARCH_MAX_RESULTS
_active_search_proxy: str = ""
_active_search_api_key: str = ""
_active_search_use_env_proxy: bool = False
_active_search_fallback_policy: str = "off"
_active_search_diagnostics: bool = False


def configure_search(
    provider_name: str,
    max_results: int = DEFAULT_SEARCH_MAX_RESULTS,
    *,
    api_key: str = "",
    api_key_env: str = "",
    proxy: str = "",
    use_env_proxy: bool = False,
    fallback_policy: str = "off",
    diagnostics: bool = False,
) -> None:
    from opensquilla.search.runtime_config import configure_search_runtime

    global _active_provider, _active_max_results, _active_search_proxy
    global _active_search_api_key, _active_search_use_env_proxy, _active_search_fallback_policy
    global _active_search_diagnostics
    _active_provider = provider_name
    _active_max_results = max_results
    _active_search_api_key = api_key.strip()
    _active_search_proxy = proxy.strip()
    _active_search_use_env_proxy = bool(use_env_proxy)
    _active_search_fallback_policy = (
        fallback_policy if fallback_policy in {"off", "network"} else "off"
    )
    _active_search_diagnostics = bool(diagnostics)
    configure_search_runtime(
        provider=provider_name,
        max_results=max_results,
        api_key=api_key,
        api_key_env=api_key_env,
        proxy=proxy,
        use_env_proxy=use_env_proxy,
        fallback_policy=fallback_policy,
        diagnostics=diagnostics,
    )


def reset_search_runtime() -> None:
    """Restore process-wide search configuration to boot defaults."""
    configure_search("duckduckgo")


def get_active_provider() -> str:
    return _active_provider


def is_search_api_key_configured(provider_name: str | None = None) -> bool:
    try:
        from opensquilla.search.runtime_config import get_resolved_search_runtime

        provider = provider_name or _active_provider
        return get_resolved_search_runtime().provider_config(provider).credential_configured
    except Exception:
        return False


def get_search_proxy() -> str:
    return _active_search_proxy


def get_search_use_env_proxy() -> bool:
    return _active_search_use_env_proxy


def get_search_fallback_policy() -> str:
    return _active_search_fallback_policy


def get_search_diagnostics() -> bool:
    return _active_search_diagnostics


def _format_search_error(provider_name: str, exc: Exception) -> tuple[str, str]:
    error_class = type(exc).__name__
    raw = str(exc).strip()
    if raw:
        return error_class, raw
    if error_class == "ConnectTimeout":
        return (
            error_class,
            (
                f"{provider_name} search request timed out. Configure search_proxy "
                "or switch search_provider to duckduckgo."
            ),
        )
    return error_class, f"{provider_name} search failed with {error_class}."


def _search_provider_kwargs(provider_name: str) -> dict[str, object]:
    from opensquilla.search.runtime_config import get_resolved_search_runtime

    kwargs = dict(get_resolved_search_runtime().provider_kwargs(provider_name))
    managed_proxy = current_managed_network_proxy_url()
    if managed_proxy:
        kwargs["proxy"] = managed_proxy
        kwargs["use_env_proxy"] = False
    return kwargs


def _ensure_builtin_search_providers() -> None:
    import opensquilla.search.providers.bocha  # noqa: F401
    import opensquilla.search.providers.brave  # noqa: F401
    import opensquilla.search.providers.duckduckgo  # noqa: F401
    import opensquilla.search.providers.exa  # noqa: F401
    import opensquilla.search.providers.iqs  # noqa: F401
    import opensquilla.search.providers.tavily  # noqa: F401


def _search_success_payload(payload: dict) -> dict:
    result = dict(payload)
    result["ok"] = True
    if "fallback_from" in result:
        result["fallbackFrom"] = result["fallback_from"]
    return result


def _search_failure_payload(payload: dict, *, retryable: bool = False) -> dict:
    result = dict(payload)
    message = str(result.get("error") or "")
    error_kind = str(result.get("error_kind") or "unknown")
    error_class = str(result.get("error_class") or "")
    result["ok"] = False
    result["retry_allowed"] = False
    result["errorMessage"] = message
    result["error"] = {
        "kind": error_kind,
        "class": error_class,
        "message": message,
        "retryable": retryable,
    }
    return result


def search_runtime_status(provider_name: str | None = None) -> dict:
    from opensquilla.search.registry import get_provider, get_provider_spec
    from opensquilla.search.runtime_config import get_resolved_search_runtime

    _ensure_builtin_search_providers()
    runtime = get_resolved_search_runtime()
    provider = provider_name or _active_provider
    spec = get_provider_spec(provider)
    provider_runtime = runtime.provider_config(provider)
    api_key_configured = provider_runtime.credential_configured
    configured = (not spec.requires_api_key) or api_key_configured
    error: str | None = None
    buildable = False
    try:
        get_provider(provider, **provider_runtime.provider_kwargs())
        buildable = True
    except Exception as exc:  # noqa: BLE001 - diagnostic surface
        error = str(exc)
    return {
        "activeProvider": _active_provider,
        "provider": provider,
        "configured": configured,
        "runtimeSupported": spec.runtime_supported,
        "requiresApiKey": spec.requires_api_key,
        "apiKeyConfigured": api_key_configured,
        "maxResults": _active_max_results,
        "proxyConfigured": bool(_active_search_proxy),
        "useEnvProxy": bool(_active_search_use_env_proxy),
        "fallbackPolicy": _active_search_fallback_policy,
        "diagnostics": bool(_active_search_diagnostics),
        "buildable": buildable,
        "error": error,
        "effectiveCredentialSource": provider_runtime.credential_source,
        "available": provider_runtime.available,
        "skippedReason": provider_runtime.skipped_reason,
        "capabilities": sorted(provider_runtime.capabilities),
        "candidateProviders": list(runtime.provider_order(SearchOptions(query="status"))),
    }


async def run_web_discover_payload(
    query: str,
    max_results: int | None = None,
    *,
    provider_name: str | None = None,
) -> dict:
    from opensquilla.search.registry import get_provider
    from opensquilla.search.runtime_config import get_resolved_search_runtime

    _ensure_builtin_search_providers()
    display_provider = provider_name or _active_provider
    runtime = get_resolved_search_runtime()
    marker = _sensitive_body_marker(query)
    if marker is not None:
        return _search_failure_payload(
            {
                "query": "[redacted]",
                "provider": display_provider,
                "results": [],
                "error_class": "SensitiveInput",
                "error": _sensitive_body_block("web_discover", marker),
                "error_kind": "invalid_request",
            },
            retryable=False,
        )
    blocklist_terms = _search_benchmark_blocklist_terms()
    if _search_benchmark_blocklist_matches(query, blocklist_terms):
        return _search_success_payload(
            _search_benchmark_blocked_query_payload(query, display_provider)
        )

    # Defence in depth: clamp to the shared ceiling before hitting the provider so
    # an out-of-range configured/active value cannot ask an uncapped provider
    # (e.g. duckduckgo) for an unbounded number of results.
    limit = min(max(max_results or _active_max_results, 1), MAX_SEARCH_RESULTS)
    effective_provider = provider_name
    if effective_provider is None and _active_provider not in _VALID_SEARCH_PROVIDERS:
        effective_provider = _active_provider
    options = SearchOptions(
        query=query,
        max_results=limit,
        fetch_top_k=0,
        provider=effective_provider,
    )
    blocklist_meta: dict[str, Any] = {}

    def provider_factory(candidate_provider: str) -> Any:
        provider = get_provider(
            candidate_provider,
            **_search_provider_kwargs(candidate_provider),
        )
        if not blocklist_terms:
            return provider
        return _BenchmarkBlocklistSearchProvider(
            provider,
            terms=blocklist_terms,
            meta=blocklist_meta,
        )

    canonical = await run_canonical_web_search(
        options,
        runtime=runtime,
        provider_factory=provider_factory,
    )
    canonical.update(blocklist_meta)
    return _web_discover_payload_from_canonical(canonical, display_provider=display_provider)


def _web_discover_payload_from_canonical(
    payload: Mapping[str, Any],
    *,
    display_provider: str,
) -> dict[str, Any]:
    diagnostics = payload.get("diagnostics")
    diagnostic_payload = diagnostics if isinstance(diagnostics, Mapping) else {}
    raw_attempts = payload.get("provider_attempts")
    attempts = list(raw_attempts) if isinstance(raw_attempts, list) else []
    selected_provider = str(diagnostic_payload.get("selected_provider") or "")
    terminal_provider = _last_search_attempt_provider(attempts)
    provider = selected_provider or terminal_provider or display_provider
    result: dict[str, Any] = {
        "query": str(payload.get("query") or ""),
        "provider": provider,
        "results": [],
    }
    if _active_search_diagnostics:
        result["attempts"] = attempts
    for key in ("benchmark_blocklist_enabled", "blocked_query", "blocked_count"):
        if key in payload:
            result[key] = payload[key]

    if payload.get("ok") is True:
        raw_results = payload.get("results")
        if isinstance(raw_results, list):
            result["results"] = [
                _lightweight_search_result(item)
                for item in raw_results
                if isinstance(item, Mapping)
            ]
        fallback_from = str(diagnostic_payload.get("fallback_from") or "")
        if fallback_from:
            result["fallback_from"] = fallback_from
        return _search_success_payload(result)

    result.update(
        {
            "error_class": str(payload.get("error_class") or "SearchProviderError"),
            "error_kind": str(payload.get("error_kind") or "unknown"),
            "error": str(payload.get("error") or "Search provider failed."),
            "provider_attempts": attempts,
            "diagnostics": dict(diagnostic_payload),
        }
    )
    return _search_failure_payload(
        result,
        retryable=bool(payload.get("provider_retryable")),
    )


def _last_search_attempt_provider(attempts: list[Any]) -> str:
    for attempt in reversed(attempts):
        if isinstance(attempt, Mapping):
            provider = str(attempt.get("provider") or "")
            if provider:
                return provider
    return ""


def _lightweight_search_result(item: Mapping[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {
        "title": str(item.get("title") or ""),
        "url": str(item.get("url") or ""),
        "snippet": str(item.get("snippet") or ""),
    }
    for key in ("provider", "published_at", "score", "domain", "canonical_url"):
        value = item.get(key)
        if value not in (None, ""):
            result[key] = value
    return result


async def run_web_search_payload(
    query: str,
    max_results: int | None = None,
    *,
    mode: str = "auto",
    fetch_top_k: int | None = None,
    max_chars_per_source: int | None = None,
    include_domains: list[str] | tuple[str, ...] | None = None,
    exclude_domains: list[str] | tuple[str, ...] | None = None,
    recency: str | None = None,
    provider: str | None = None,
) -> dict[str, object]:
    if not isinstance(query, str) or not query.strip():
        return _invalid_search_request_payload("query must be a non-empty string.")
    marker = _sensitive_body_marker(query)
    if marker is not None:
        return _search_failure_payload(
            {
                "query": "[redacted]",
                "provider": provider or _active_provider,
                "sources": [],
                "results": [],
                "error_class": "SensitiveInput",
                "error": _sensitive_body_block("web_search", marker),
                "error_kind": "invalid_request",
            },
            retryable=False,
        )
    if mode not in _VALID_SEARCH_MODES:
        expected = ", ".join(sorted(_VALID_SEARCH_MODES))
        return _invalid_search_request_payload(f"Invalid mode. Expected one of: {expected}.")
    if recency is not None and recency not in _VALID_SEARCH_RECENCIES:
        expected = ", ".join(sorted(_VALID_SEARCH_RECENCIES))
        return _invalid_search_request_payload(
            f"Invalid recency. Expected one of: {expected}."
        )
    if provider is not None and provider not in _VALID_SEARCH_PROVIDERS:
        expected = ", ".join(sorted(_VALID_SEARCH_PROVIDERS))
        return _invalid_search_request_payload(
            f"Invalid provider. Expected one of: {expected}."
        )

    resolved_max_results, error = _optional_search_int(max_results, "max_results")
    if error is not None:
        return _invalid_search_request_payload(error)
    resolved_fetch_top_k, error = _optional_search_int(fetch_top_k, "fetch_top_k")
    if error is not None:
        return _invalid_search_request_payload(error)
    resolved_max_chars, error = _optional_search_int(
        max_chars_per_source,
        "max_chars_per_source",
    )
    if error is not None:
        return _invalid_search_request_payload(error)
    resolved_include_domains, error = _search_domain_list(
        include_domains,
        "include_domains",
    )
    if error is not None:
        return _invalid_search_request_payload(error)
    resolved_exclude_domains, error = _search_domain_list(
        exclude_domains,
        "exclude_domains",
    )
    if error is not None:
        return _invalid_search_request_payload(error)

    options = SearchOptions(
        query=query,
        mode=cast(SearchMode, mode),
        max_results=(
            _active_max_results if resolved_max_results is None else resolved_max_results
        ),
        fetch_top_k=3 if resolved_fetch_top_k is None else resolved_fetch_top_k,
        max_chars_per_source=1500 if resolved_max_chars is None else resolved_max_chars,
        include_domains=resolved_include_domains,
        exclude_domains=resolved_exclude_domains,
        recency=cast(Recency | None, recency),
        provider=None if provider in (None, "auto") else provider,
    )
    blocklist_terms = _search_benchmark_blocklist_terms()
    if _search_benchmark_blocklist_matches(query, blocklist_terms):
        provider_name = provider or _active_provider
        payload = _search_benchmark_blocked_query_payload(query, provider_name)
        payload.update(
            {
                "ok": True,
                "mode": mode,
                "provider_attempts": [],
                "diagnostics": {
                    "selected_provider": provider_name,
                    "benchmark_blocklist_enabled": True,
                    "blocked_query": True,
                    "blocked_count": 0,
                },
                "sources": [],
            }
        )
        return payload
    if not blocklist_terms:
        return await run_canonical_web_search(options, fetcher=_web_search_fetcher)

    from opensquilla.search.runtime_config import get_resolved_search_runtime

    runtime = get_resolved_search_runtime()
    blocklist_meta: dict[str, Any] = {
        "benchmark_blocklist_enabled": True,
        "blocked_count": 0,
    }

    def provider_factory(provider_name: str) -> _BenchmarkBlocklistSearchProvider:
        return _BenchmarkBlocklistSearchProvider(
            runtime.build_provider(provider_name),
            terms=blocklist_terms,
            meta=blocklist_meta,
        )

    payload = await run_canonical_web_search(
        options,
        runtime=runtime,
        provider_factory=provider_factory,
        fetcher=_web_search_fetcher,
    )
    payload.update(blocklist_meta)
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, dict):
        diagnostics["benchmark_blocklist_enabled"] = True
        diagnostics["blocked_count"] = blocklist_meta["blocked_count"]
    return payload


def _invalid_search_request_payload(message: str) -> dict[str, object]:
    return {
        "ok": False,
        "error_kind": "invalid_request",
        "error": message,
        "retry_allowed": False,
    }


def _optional_search_int(value: object, name: str) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{name} must be an integer."
    return value, None


def _search_domain_list(value: object, name: str) -> tuple[tuple[str, ...], str | None]:
    if value is None:
        return (), None
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        return (), f"{name} must be a list or tuple of strings."
    return tuple(value), None


async def _web_search_fetcher(url: str, max_chars: int) -> dict[str, object]:
    from opensquilla.tools.builtin.web_fetch import run_web_fetch_payload

    return await run_web_fetch_payload(url, max_chars=max_chars)


def _classify_search_error(provider_name: str, exc: Exception) -> SearchProviderError | None:
    if isinstance(exc, SearchProviderError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return SearchProviderError(
            provider=provider_name,
            kind="timeout",
            message=str(exc) or "Search request timed out.",
            retryable=True,
        )
    if isinstance(exc, httpx.NetworkError):
        return SearchProviderError(
            provider=provider_name,
            kind="network",
            message=str(exc) or "Search network request failed.",
            retryable=True,
        )
    return None


def _search_payload(
    query: str,
    provider_name: str,
    results: list[SearchResult],
    *,
    fallback_from: str = "",
    attempts: list[dict[str, str]] | None = None,
) -> dict:
    payload = {
        "query": query,
        "provider": provider_name,
        "results": [_search_result_payload(provider_name, r) for r in results],
    }
    if fallback_from:
        payload["fallback_from"] = fallback_from
    if attempts is not None:
        payload["attempts"] = attempts
    return payload


def _search_result_payload(provider_name: str, result: SearchResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": result.title,
        "url": result.url,
        "snippet": result.snippet,
    }
    provider = result.provider or result.source or provider_name
    if provider:
        payload["provider"] = provider
    if result.published_at:
        payload["published_at"] = result.published_at
    if result.score is not None:
        payload["score"] = result.score
    if result.url:
        domain = extract_domain(result.url)
        canonical_url = canonicalize_url(result.url)
        if domain:
            payload["domain"] = domain
        if canonical_url:
            payload["canonical_url"] = canonical_url
    return payload


def _search_error_payload(
    query: str,
    provider_name: str,
    exc: Exception,
    *,
    attempts: list[dict[str, str]] | None = None,
) -> dict:
    error_class, error_message = _format_search_error(provider_name, exc)
    payload: dict[str, Any] = {
        "query": query,
        "provider": provider_name,
        "results": [],
        "error_class": error_class,
        "error": error_message,
    }
    classified = _classify_search_error(provider_name, exc)
    if classified is not None:
        payload["error_kind"] = classified.kind
    if attempts is not None:
        payload["attempts"] = attempts
    return payload


@tool(
    name="web_search",
    description=(
        "Source-backed web search for current information. Searches, deduplicates, "
        "and can fetch compact citation-ready excerpts from top sources."
    ),
    params={
        "query": {"type": "string", "description": "Search query."},
        "mode": {
            "type": "string",
            "description": "Search mode.",
            "enum": ["auto", "news", "technical", "broad"],
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of deduplicated results to return.",
        },
        "fetch_top_k": {
            "type": "integer",
            "description": "Number of top results to fetch for compact excerpts.",
        },
        "max_chars_per_source": {
            "type": "integer",
            "description": "Maximum excerpt characters per source.",
        },
        "include_domains": {
            "type": "array",
            "description": "Optional domains to include.",
            "items": {"type": "string"},
        },
        "exclude_domains": {
            "type": "array",
            "description": "Optional domains to exclude.",
            "items": {"type": "string"},
        },
        "recency": {
            "type": "string",
            "description": "Optional recency filter.",
            "enum": ["day", "week", "month", "year"],
        },
        "provider": {
            "type": "string",
            "description": "Optional provider override.",
            "enum": ["auto", "bocha", "tavily", "brave", "duckduckgo", "exa", "iqs"],
        },
    },
    required=["query"],
    result_budget_class="external",
    sandbox=SandboxToolDescriptor.network(
        kind="web.fetch",
        argv_factory=_web_search_sandbox_argv,
        request_factory=_network_search_request,
        record_payload=False,
    ),
)
async def web_search(
    query: str,
    mode: str = "auto",
    max_results: int | None = None,
    fetch_top_k: int | None = None,
    max_chars_per_source: int | None = None,
    include_domains: list[str] | tuple[str, ...] | None = None,
    exclude_domains: list[str] | tuple[str, ...] | None = None,
    recency: str | None = None,
    provider: str | None = None,
) -> str:
    payload = await run_web_search_payload(
        query,
        mode=mode,
        max_results=max_results,
        fetch_top_k=fetch_top_k,
        max_chars_per_source=max_chars_per_source,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        recency=recency,
        provider=provider,
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


@tool(
    name="web_discover",
    description="Lightweight web link discovery that returns titles, URLs, and snippets.",
    params={
        "query": {"type": "string", "description": "Search query."},
        "max_results": {
            "type": "integer",
            "description": "Maximum number of results to return.",
        },
    },
    required=["query"],
    result_budget_class="external",
    sandbox=SandboxToolDescriptor.network(
        kind="web.fetch",
        argv_factory=_web_discover_sandbox_argv,
        request_factory=_network_search_request,
        record_payload=False,
    ),
)
async def web_discover(query: str, max_results: int | None = None) -> str:
    payload = await run_web_discover_payload(query, max_results)
    tool_payload = dict(payload)
    if tool_payload.get("ok") is True:
        tool_payload.pop("ok", None)
        tool_payload.pop("fallbackFrom", None)
        tool_payload.pop("errorMessage", None)
    if isinstance(tool_payload.get("error"), dict):
        tool_payload["error"] = tool_payload["error"].get("message", "")
    return json.dumps(tool_payload, ensure_ascii=False, indent=2)
