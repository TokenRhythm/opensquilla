"""Clone-only provider selection for independent auxiliary features."""

from __future__ import annotations


def effective_session_model(session: object | None) -> str | None:
    """Return a recorded/session model for features such as auto-naming."""
    return (
        str(getattr(session, "model_override", "") or "").strip()
        or str(getattr(session, "model", "") or "").strip()
        or None
    )


def resolve_selected_provider(
    ctx: object, session: object | None, *, model_override: str | None = None,
) -> object | None:
    """Resolve a selector clone, optionally using an auxiliary model override.

    This seam is independent of compaction's current-model-only contract. A
    missing clone can resolve the existing provider, but cannot mutate its model.
    """
    selector = getattr(ctx, "provider_selector", None)
    if selector is None:
        return None
    resolved_selector = selector
    clone = getattr(selector, "clone", None)
    if callable(clone):
        try:
            resolved_selector = clone()
        except Exception:
            pass
    model = str(model_override or effective_session_model(session) or "").strip()
    if model and resolved_selector is not selector:
        override = getattr(resolved_selector, "override_model", None)
        if callable(override):
            try:
                override(model)
            except Exception:
                return None
    resolver = getattr(resolved_selector, "resolve", None)
    if not callable(resolver):
        return None
    try:
        return resolver()
    except Exception:
        return None
