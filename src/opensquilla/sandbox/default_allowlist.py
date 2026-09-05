"""Built-in managed-network allowlist entries."""

from __future__ import annotations

from opensquilla.sandbox.domain_validation import domain_matches

DEFAULT_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "github": (
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "github.githubassets.com",
        "avatars.githubusercontent.com",
        "uploads.github.com",
        "release-assets.githubusercontent.com",
        "ghcr.io",
        "pkg-containers.githubusercontent.com",
    ),
    "search": (
        "api.search.brave.com",
        "html.duckduckgo.com",
        "duckduckgo.com",
        "api.bochaai.com",
        "api.exa.ai",
        "api.serpdive.com",
        "api.tavily.com",
        "cloud-iqs.aliyuncs.com",
        "www.google.com",
        "www.bing.com",
    ),
    "developer-docs": (
        "developer.mozilla.org",
        "docs.python.org",
        "docs.npmjs.com",
        "doc.rust-lang.org",
        "go.dev",
    ),
}


def default_allowlist_source(host: str) -> str | None:
    for group, domains in DEFAULT_ALLOWLIST.items():
        if any(domain_matches(domain, host) for domain in domains):
            return f"default:{group}"
    return None


def default_allowlist_payload() -> list[dict[str, object]]:
    return [
        {
            "group": group,
            "domains": list(domains),
            "read_only": True,
        }
        for group, domains in DEFAULT_ALLOWLIST.items()
    ]


__all__ = [
    "DEFAULT_ALLOWLIST",
    "default_allowlist_payload",
    "default_allowlist_source",
]
