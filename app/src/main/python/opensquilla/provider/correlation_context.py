"""Task-local carrier for provider request correlation.

The explicit ``ChatConfig.provider_request_correlation`` field remains the
transport source of truth.  This narrow ContextVar exists so in-process tool
callbacks and auxiliary helpers can inherit the current execution identity
without coupling that identity to usage accounting.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .types import ProviderRequestCorrelation

_ACTIVE_PROVIDER_REQUEST_CORRELATION: ContextVar[
    ProviderRequestCorrelation | None
] = ContextVar(
    "opensquilla_provider_request_correlation",
    default=None,
)


def current_provider_request_correlation() -> ProviderRequestCorrelation | None:
    """Return the provider correlation bound to the current task."""

    return _ACTIVE_PROVIDER_REQUEST_CORRELATION.get()


@contextmanager
def bind_provider_request_correlation(
    correlation: ProviderRequestCorrelation | None,
) -> Iterator[None]:
    """Bind ``correlation`` for a narrow internal call scope.

    ``None`` is deliberately bound rather than treated as a no-op.  This lets
    non-session work explicitly suppress an inherited turn correlation.
    """

    token = _ACTIVE_PROVIDER_REQUEST_CORRELATION.set(correlation)
    try:
        yield
    finally:
        _ACTIVE_PROVIDER_REQUEST_CORRELATION.reset(token)


__all__ = [
    "bind_provider_request_correlation",
    "current_provider_request_correlation",
]
