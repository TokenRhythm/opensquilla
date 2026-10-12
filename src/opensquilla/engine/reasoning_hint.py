"""Model-family reasoning format hints."""

from __future__ import annotations

_REASONING_HINT = (
    "For reasoning-capable models, keep private reasoning inside "
    "<think>...</think> when needed and put the user-visible answer inside "
    "<final>...</final>."
)

_REASONING_MODEL_MARKERS = (
    "gpt-5",
    "codex",
    "glm-4.7",
    "glm-4.6",
    "deepseek-r1",
)


def model_family(resolved_model: str) -> str | None:
    """Return the reasoning-capable model family for a resolved model id."""

    normalized = resolved_model.strip().lower()
    if not normalized:
        return None
    for marker in _REASONING_MODEL_MARKERS:
        if marker in normalized:
            return marker
    return None


def reasoning_tag_hint(resolved_model: str) -> str | None:
    """Return the prompt hint for reasoning-capable models."""

    return _REASONING_HINT if model_family(resolved_model) is not None else None
