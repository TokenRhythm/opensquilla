"""Transport-neutral runtime primitives for setup configuration mutations.

The Gateway and agent tools both mutate the same live ``GatewayConfig``.  This
module owns the durable-candidate and live-install mechanics so neither caller
needs to import an RPC handler to get the transaction semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opensquilla.application.config_secrets import inherit_runtime_secrets

if TYPE_CHECKING:
    from opensquilla.onboarding.config_store import CredentialBackupRedaction


def active_gateway_config(holder: Any) -> Any:
    """Return the holder's live config, falling back to the configured store."""

    config = getattr(holder, "config", None)
    if config is not None:
        return config
    from opensquilla.onboarding.config_store import load_config

    return load_config()


def gateway_config_path(_holder: Any, source: Any) -> str | None:
    """Resolve the persistence path associated with a config candidate."""

    path = getattr(source, "config_path", None)
    return str(path) if path else None


def install_gateway_config_candidate(holder: Any, candidate: Any) -> None:
    """Install a persisted candidate into the existing live config object."""

    current = getattr(holder, "config", None)
    if current is None or current is candidate:
        return
    for field_name in type(candidate).model_fields:
        setattr(current, field_name, getattr(candidate, field_name))
    inherit_runtime_secrets(candidate, current)
    if hasattr(current, "inherit_persist_provenance") and hasattr(
        candidate, "_runtime_field_overrides"
    ):
        current.inherit_persist_provenance(candidate)


def persist_setup_candidate(
    holder: Any,
    candidate: Any,
    *,
    restart_required: bool,
    backup_credential_redaction: CredentialBackupRedaction | None = None,
    remove_paths: tuple[str, ...] = (),
) -> str:
    """Durably persist a detached setup candidate without touching live state."""

    from opensquilla.onboarding.config_store import persist_config

    current = getattr(holder, "config", None)
    path = gateway_config_path(holder, candidate) or gateway_config_path(holder, current)
    persisted = persist_config(
        candidate,
        path=path,
        restart_required=restart_required,
        backup_credential_redaction=backup_credential_redaction,
        remove_paths=remove_paths,
    )
    if hasattr(candidate, "config_path") and not getattr(candidate, "config_path", None):
        candidate.config_path = str(persisted.path)
    if (
        current is not None
        and hasattr(current, "config_path")
        and not getattr(current, "config_path", None)
    ):
        current.config_path = str(persisted.path)
    return str(persisted.path)


def sync_media_runtime(config: Any) -> None:
    """Apply image-generation and audio configuration to the live tool layer."""

    from opensquilla.tools.builtin.media import configure_audio, configure_image_generation

    configure_image_generation(
        getattr(config, "image_generation", None),
        gateway_config=config,
        llm_config=getattr(config, "llm", None),
        squilla_router_config=getattr(config, "squilla_router", None),
    )
    configure_audio(getattr(config, "audio", None))


__all__ = [
    "active_gateway_config",
    "gateway_config_path",
    "install_gateway_config_candidate",
    "persist_setup_candidate",
    "sync_media_runtime",
]
