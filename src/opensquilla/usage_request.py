"""Context-bound accounting hook for HTTP adapters without engine dependencies."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_RequestAccounting = Callable[..., Awaitable[Any]]
_ACCOUNT_REQUEST: ContextVar[_RequestAccounting | None] = ContextVar(
    "opensquilla_request_accounting", default=None
)


@contextmanager
def bind_request_accounting(handler: _RequestAccounting) -> Iterator[None]:
    token = _ACCOUNT_REQUEST.set(handler)
    try:
        yield
    finally:
        _ACCOUNT_REQUEST.reset(token)


async def accounted_request(
    client: Any,
    method: str,
    url: str,
    *,
    provider: str,
    model: str = "",
    operation: str,
    base_url: str = "",
    requested_meters: tuple[Any, ...] = (),
    **kwargs: Any,
) -> Any:
    handler = _ACCOUNT_REQUEST.get()
    if handler is None:
        return await getattr(client, method.lower())(url, **kwargs)
    return await handler(
        client,
        method,
        url,
        provider=provider,
        model=model,
        operation=operation,
        base_url=base_url,
        requested_meters=requested_meters,
        **kwargs,
    )
