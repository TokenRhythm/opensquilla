"""Source selection shared by all Community Skill installation surfaces."""

from __future__ import annotations

from urllib.parse import urlsplit

_GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "raw.githubusercontent.com"})


def resolve_install_source(identifier: str, source: str | None = None) -> str:
    """Infer only explicit GitHub URLs; keep ambiguous registry slugs unchanged."""
    if source is not None:
        return source.strip() or "clawhub"
    value = identifier.strip()
    if value.startswith("github.com/"):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "clawhub"
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() in _GITHUB_HOSTS:
        return "github"
    return "clawhub"
