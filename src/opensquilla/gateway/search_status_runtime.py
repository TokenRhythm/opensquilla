"""Shared search readiness projection for RPC and diagnostics adapters."""

from __future__ import annotations

from typing import Any

from opensquilla.sandbox.integration import in_process_network_precondition
from opensquilla.tools.builtin.web import search_runtime_status


def read_search_status(provider_id: str | None = None) -> dict[str, Any]:
    payload = search_runtime_status(provider_id)
    reason = in_process_network_precondition()
    payload["networkReady"] = reason is None
    payload["networkBlockedReason"] = reason
    return payload
