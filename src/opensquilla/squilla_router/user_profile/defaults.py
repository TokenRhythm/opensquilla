"""Default shape for profiles produced from conversation history."""

from __future__ import annotations

import copy
from typing import Any

_DEFAULT_USER_PROFILE: dict[str, Any] = {
    "profile_version": "default",
    "permission": {
        "allow_models": [],
        "deny_models": [],
        "allow_tools": [],
        "risk_allowlist": ["low", "medium", "high"],
    },
    "preference": {
        "quality_latency_tradeoff": "balanced",
        "cost_sensitivity": "medium",
    },
    "history": {
        "positive_model_ids": [],
        "negative_model_ids": [],
        "feedback_count": 0,
        "last_updated_at": "",
        "capability_prior": {},
    },
}


def default_user_profile() -> dict[str, Any]:
    """Return an independent copy of the producer's complete output shape."""

    return copy.deepcopy(_DEFAULT_USER_PROFILE)


__all__ = ["default_user_profile"]
