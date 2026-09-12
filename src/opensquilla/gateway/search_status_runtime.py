"""Shared search readiness projection for RPC and diagnostics adapters."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from opensquilla.sandbox.integration import in_process_network_precondition
from opensquilla.tools.builtin.web import search_runtime_status


def read_search_status(
    provider_id: str | None = None,
    *,
    probe_context: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
) -> dict[str, Any]:
    """Project search readiness, including the network posture a query will meet.

    `in_process_network_precondition()` answers for whatever Run Context is
    current. A caller that will run the query under a context of its own has to
    hand that context in, or the two disagree: readiness reports the refusal the
    query no longer meets, which is issue #1202 read from the other side. Both
    callers of this function reach the same in-process path, so both pass one.
    """

    payload = search_runtime_status(provider_id)
    scope: contextlib.AbstractContextManager[None] = (
        probe_context() if probe_context is not None else contextlib.nullcontext()
    )
    with scope:
        reason = in_process_network_precondition()
    payload["networkReady"] = reason is None
    payload["networkBlockedReason"] = reason
    return payload
