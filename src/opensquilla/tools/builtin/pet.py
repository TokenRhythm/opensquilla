"""Desktop-pet tools: pet_speak / pet_notify (PRD F2 tool surface).

These drive the running :class:`~opensquilla.pet.service.PetReminderService`
(injected by the gateway boot path). When ``[pet]`` is disabled or the service
is not running they return an explicit ``not_available`` envelope rather than
failing the turn. Message text is redacted and truncated by the service, so raw
session content is never echoed to the pet (PRD risk R6).
"""

from __future__ import annotations

import json
from typing import Any

from opensquilla.tools.registry import tool

_pet_service: Any = None


def set_pet_service(service: Any) -> None:
    """Bind (or clear) the live pet service used by the pet tools."""
    global _pet_service
    _pet_service = service


def _not_available(note: str) -> str:
    return json.dumps(
        {"status": "not_available", "capability": "desktop_pet", "note": note}
    )


@tool(
    name="pet_speak",
    description=(
        "Speak a short message aloud through the desktop pet (AgentRMC). "
        "Use for brief spoken alerts; text is redacted and truncated. "
        "Returns not_available when the pet service is disabled or unreachable."
    ),
    params={
        "message": {
            "type": "string",
            "description": "Short text to speak (keep it concise; truncated to ~120 chars).",
        },
    },
    required=["message"],
)
async def pet_speak(message: str) -> str:
    if _pet_service is None or not getattr(_pet_service, "running", False):
        return _not_available("Desktop pet service is not enabled ([pet].enabled).")
    if not message or not message.strip():
        return _not_available("message must not be empty.")
    ok = await _pet_service.speak(message)
    if not ok:
        return _not_available("The desktop pet is unreachable; nothing was spoken.")
    return json.dumps({"status": "ok", "spoken": True})


@tool(
    name="pet_notify",
    description=(
        "Send a short bubble notification to the desktop pet (AgentRMC), "
        "optionally speaking it. Text is redacted and truncated. "
        "Returns not_available when the pet service is disabled or unreachable."
    ),
    params={
        "message": {
            "type": "string",
            "description": "Short notification text (truncated to ~120 chars).",
        },
    },
    required=["message"],
)
async def pet_notify(message: str) -> str:
    if _pet_service is None or not getattr(_pet_service, "running", False):
        return _not_available("Desktop pet service is not enabled ([pet].enabled).")
    if not message or not message.strip():
        return _not_available("message must not be empty.")
    ok = await _pet_service.notify_text(message)
    if not ok:
        return _not_available("The desktop pet is unreachable; notification spooled or lost.")
    return json.dumps({"status": "ok", "notified": True})


__all__ = ["pet_notify", "pet_speak", "set_pet_service"]
