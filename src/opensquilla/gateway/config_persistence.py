"""Shared durable persistence boundary for Gateway configuration mutations."""

from __future__ import annotations

from typing import Any

import structlog

from opensquilla.paths import default_opensquilla_home

log = structlog.get_logger(__name__)


def persist_gateway_config(config: Any) -> None:
    """Persist one validated config candidate without exposing an RPC handler.

    The onboarding store owns sparse TOML merge, backup, validation, fsync,
    rename, and file-mode guarantees.  Gateway mutation surfaces share this
    boundary so they never import one another's private RPC modules.
    """

    if not getattr(config, "config_path", None) and hasattr(config, "config_path"):
        config.config_path = str(default_opensquilla_home() / "config.toml")

    if not getattr(config, "config_path", None):
        return

    from opensquilla.onboarding.config_store import persist_config

    path = str(config.config_path)
    try:
        result = persist_config(config, path=path)
    except Exception as exc:
        # Validation failures may contain rejected secret values.  Log only
        # the exception type at this boundary.
        log.error(
            "gateway.config_persist_failed",
            path=path,
            error=type(exc).__name__,
        )
        raise
    log.info(
        "gateway.config_persisted",
        path=str(result.path),
        backup=str(result.backup_path) if result.backup_path else None,
    )


__all__ = ["persist_gateway_config"]
