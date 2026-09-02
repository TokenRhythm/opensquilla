"""Application Module for setup status and catalog use cases.

Setup mutations are intentionally not collapsed into a generic ``configure``
or ``reset`` operation.  Their domain Implementations remain the explicit
functions in :mod:`opensquilla.onboarding.mutations`; the Gateway owns their
persistence and live-runtime reconciliation.  This Module provides the shared
read seam used by WebUI setup consumers without importing ``RpcContext``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class SetupCatalogPort(Protocol):
    async def load_setup_catalog(self) -> Mapping[str, Any]: ...


class SetupStatusPort(Protocol):
    async def load_setup_status(self) -> Mapping[str, Any]: ...


class SetupWorkflow:
    """Expose real setup read use cases over explicit Ports."""

    def __init__(self, catalog: SetupCatalogPort, status: SetupStatusPort) -> None:
        self._catalog = catalog
        self._status = status

    async def catalog(self) -> dict[str, Any]:
        return dict(await self._catalog.load_setup_catalog())

    async def status(self) -> dict[str, Any]:
        return dict(await self._status.load_setup_status())
