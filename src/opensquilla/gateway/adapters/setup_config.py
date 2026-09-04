"""Gateway Adapter for durable setup configuration candidates."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from opensquilla.gateway.setup_config_runtime import (
    active_gateway_config,
    install_gateway_config_candidate,
    persist_setup_candidate,
)


class GatewaySetupConfigPort:
    """Adapt any holder with a live ``config`` field to ``SetupConfigPort``."""

    def __init__(self, holder: Any) -> None:
        self._holder = holder

    def active_config(self) -> Any:
        return active_gateway_config(self._holder)

    def persist_candidate(
        self,
        candidate: Any,
        *,
        restart_required: bool,
        backup_credential_provider: str | None = None,
        remove_paths: Sequence[str] = (),
    ) -> str:
        backup_redaction = None
        if backup_credential_provider:
            from opensquilla.onboarding.config_store import CredentialBackupRedaction

            backup_redaction = CredentialBackupRedaction(
                provider_id=str(backup_credential_provider).strip().lower()
            )
        return persist_setup_candidate(
            self._holder,
            candidate,
            restart_required=restart_required,
            backup_credential_redaction=backup_redaction,
            remove_paths=tuple(remove_paths),
        )

    def install_candidate(self, candidate: Any) -> Any:
        install_gateway_config_candidate(self._holder, candidate)
        return active_gateway_config(self._holder)


__all__ = ["GatewaySetupConfigPort"]
