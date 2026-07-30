"""Contract tests binding search providers to managed-network sandbox domains."""

from __future__ import annotations

import importlib
import pkgutil
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import opensquilla.search.providers as providers_pkg
from opensquilla.sandbox.default_allowlist import DEFAULT_ALLOWLIST
from opensquilla.sandbox.integration import (
    _SEARCH_PROVIDER_SYSTEM_DOMAINS,
    _system_domain_grants_for_request,
)
from opensquilla.search.registry import get_provider_spec

_PROVIDER_API_URL_ATTRS = {
    "bocha": ("opensquilla.search.providers.bocha", "_API_URL"),
    "brave": ("opensquilla.search.providers.brave", "_API_URL"),
    "duckduckgo": ("opensquilla.search.providers.duckduckgo", "_DDHTML_URL"),
    "exa": ("opensquilla.search.providers.exa", "_API_URL"),
    "iqs": ("opensquilla.search.providers.iqs", "_API_URL"),
    "tavily": ("opensquilla.search.providers.tavily", "_API_URL"),
}


def _builtin_provider_ids() -> list[str]:
    # Builtin provider modules register their spec under the module name;
    # enumerating the package (instead of the registry) keeps this contract
    # immune to fake providers registered by other tests.
    return sorted(module.name for module in pkgutil.iter_modules(providers_pkg.__path__))


def test_every_builtin_search_provider_has_sandbox_domain_grants() -> None:
    """A runtime provider without sandbox domains fails under managed network."""
    for provider_id in _builtin_provider_ids():
        importlib.import_module(f"opensquilla.search.providers.{provider_id}")
        spec = get_provider_spec(provider_id)
        if not spec.runtime_supported:
            continue
        domains = _SEARCH_PROVIDER_SYSTEM_DOMAINS.get(provider_id)
        assert domains, (
            f"search provider {provider_id!r} has no entry in "
            "_SEARCH_PROVIDER_SYSTEM_DOMAINS; managed-network sandbox runs "
            "cannot reach its API"
        )
        for domain in domains:
            assert domain in DEFAULT_ALLOWLIST["search"], (
                f"{domain!r} (provider {provider_id!r}) is missing from "
                "the default managed-network search allowlist group"
            )


@pytest.mark.parametrize(("provider_id", "url_ref"), sorted(_PROVIDER_API_URL_ATTRS.items()))
def test_search_provider_system_domains_match_provider_api_hosts(
    provider_id: str,
    url_ref: tuple[str, str],
) -> None:
    module_name, attr = url_ref
    module = importlib.import_module(module_name)
    host = urlparse(getattr(module, attr)).hostname

    assert host is not None
    assert host in _SEARCH_PROVIDER_SYSTEM_DOMAINS[provider_id]


@pytest.mark.parametrize(
    ("provider_id", "fallback_policy", "expected"),
    [
        ("bocha", "off", ("api.bochaai.com",)),
        ("exa", "off", ("api.exa.ai",)),
        ("tavily", "network", ("api.tavily.com", "html.duckduckgo.com")),
        ("duckduckgo", "network", ("html.duckduckgo.com",)),
    ],
)
def test_system_domain_grants_cover_active_keyed_provider(
    provider_id: str,
    fallback_policy: str,
    expected: tuple[str, ...],
) -> None:
    from opensquilla.tools.builtin import web

    web.configure_search(
        provider_id,
        max_results=5,
        api_key="dummy-test-key" if provider_id != "duckduckgo" else "",
        fallback_policy=fallback_policy,
    )
    try:
        request = SimpleNamespace(argv=("web_search", "{}"))
        assert _system_domain_grants_for_request(request) == expected  # type: ignore[arg-type]
    finally:
        web.reset_search_runtime()


@pytest.mark.parametrize(
    ("tool_name", "providers", "expected"),
    [
        (
            "web_search",
            "bocha,tavily",
            ("api.bochaai.com", "api.tavily.com"),
        ),
        (
            "web_discover",
            "exa,duckduckgo",
            ("api.exa.ai", "html.duckduckgo.com"),
        ),
        ("web_search", "duckduckgo", ("html.duckduckgo.com",)),
    ],
)
def test_system_domain_grants_follow_bounded_search_execution_plan(
    tool_name: str,
    providers: str,
    expected: tuple[str, ...],
) -> None:
    request = SimpleNamespace(argv=(tool_name, "query", f"providers={providers}"))

    assert _system_domain_grants_for_request(request) == expected  # type: ignore[arg-type]


def test_web_search_sandbox_argv_uses_runtime_execution_plan(monkeypatch) -> None:
    from opensquilla.tools.builtin import web

    for key in (
        "BOCHA_SEARCH_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "IQS_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    web.configure_search("duckduckgo", fallback_policy="network")
    try:
        argv = web._web_search_sandbox_argv({"query": "python release"})
        request = SimpleNamespace(argv=argv)

        assert argv[-1] == "providers=bocha,tavily"
        assert _system_domain_grants_for_request(request) == (  # type: ignore[arg-type]
            "api.bochaai.com",
            "api.tavily.com",
        )
    finally:
        web.reset_search_runtime()


def test_discover_plan_token_honors_explicit_provider_override(monkeypatch) -> None:
    from opensquilla.tools.builtin import web

    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    web.configure_search("duckduckgo", fallback_policy="network")
    try:
        token = web._search_plan_argv_token(
            {"query": "python release", "provider": "exa"},
            tool_name="web_discover",
        )
        request = SimpleNamespace(argv=("web_search", "python release", token))

        assert token == "providers=exa,duckduckgo"
        assert _system_domain_grants_for_request(request) == (  # type: ignore[arg-type]
            "api.exa.ai",
            "html.duckduckgo.com",
        )
    finally:
        web.reset_search_runtime()
