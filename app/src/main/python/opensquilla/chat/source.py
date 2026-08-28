"""Chat source metadata helpers shared by frontends."""

from __future__ import annotations

from typing import Any

from opensquilla.run_mode import normalize_run_mode


def chat_source_metadata(
    *,
    caller_kind: str,
    channel_kind: str,
    channel_id: str,
    sender_id: str,
    source_kind: str,
    source_name: str,
    elevated: str | None = None,
    run_mode: str | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "caller_kind": caller_kind,
        "channel_kind": channel_kind,
        "channel_id": channel_id,
        "sender_id": sender_id,
        "source_kind": source_kind,
        "source_name": source_name,
    }
    if elevated in ("on", "bypass", "full"):
        source["elevated"] = elevated
    if run_mode is not None:
        try:
            source["runMode"] = normalize_run_mode(run_mode).value
        except ValueError:
            pass
    return source
