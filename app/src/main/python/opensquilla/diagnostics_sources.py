"""Higher-layer data sources for the offline diagnostics bundle.

The bundle generator (``observability.bundle``) is a low-level disk reader,
but three of its artifacts compose higher-layer functionality: config
resolution (onboarding), config redaction and the logs-status snapshot
(gateway), and the offline doctor report (cli). Those imports cannot live
inside the ``observability`` package without inverting the package layering
— cli and gateway already import observability — so this top-level module
hosts the composition instead (the ``permissions.py``/``router_control.py``
precedent for cross-layer shims).

Every import here is function-level by design: importing this module (and
therefore the bundle generator) stays runtime-inert and never pulls in the
gateway or CLI stacks.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any


def resolve_config_source() -> tuple[Path, str]:
    """Return ``(config_path, source)`` with gateway-equivalent precedence."""
    from opensquilla.onboarding.config_store import resolve_config_path

    return resolve_config_path(None)


def redact_config_payload(data: Any) -> Any:
    """Mask sensitive keys in a raw config payload for safe sharing."""
    from opensquilla.gateway.config import redact_public_config

    return redact_public_config(data)


def offline_doctor_report(
    error: BaseException,
    *,
    gateway_url: str,
    config_path: str | None,
) -> dict[str, Any]:
    """Build the doctor health report without dialing a gateway."""
    from opensquilla.cli.doctor_cmd import _offline_report

    return _offline_report(error, gateway_url=gateway_url, config_path=config_path)


def logs_status_snapshot() -> dict[str, Any]:
    """Offline reconstruction of the ``logs.status`` RPC payload.

    Reports the *ambient* process environment (env vars, default state
    paths), matching what a gateway launched from this environment would see.
    """
    from opensquilla.gateway.rpc_logs import _build_logs_status

    ctx: Any = SimpleNamespace(config=None, diagnostics_state=None)
    return _build_logs_status(ctx)
