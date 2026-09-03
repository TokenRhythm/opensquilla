"""Gateway Adapter for the setup workflow read Ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from opensquilla.application.setup_workflow import SetupCatalog, SetupStatus
from opensquilla.gateway.rpc import RpcContext

SetupReader = Callable[[RpcContext], Awaitable[dict[str, Any]]]


class RpcContextSetupWorkflowPort:
    def __init__(
        self,
        ctx: RpcContext,
        *,
        catalog_reader: SetupReader,
        status_reader: SetupReader,
    ) -> None:
        self._ctx = ctx
        self._catalog_reader = catalog_reader
        self._status_reader = status_reader

    async def load_setup_catalog(self) -> SetupCatalog:
        return cast(SetupCatalog, await self._catalog_reader(self._ctx))

    async def load_setup_status(self) -> SetupStatus:
        return cast(SetupStatus, await self._status_reader(self._ctx))
