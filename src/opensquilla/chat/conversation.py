"""Frontend-neutral chat conversation request contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChatSendRequest:
    session_key: str
    message: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    display_text: str | None = None
    intent: str | None = None
    elevated: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def sessions_send_params(
    request: ChatSendRequest,
    source: dict[str, Any],
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "key": request.session_key,
        "message": request.message,
        "_source": source,
    }
    if request.attachments:
        params["attachments"] = request.attachments
    if request.display_text is not None:
        params["displayText"] = request.display_text
    if request.intent is not None:
        params["intent"] = request.intent
    params.update(request.extra)
    return params
