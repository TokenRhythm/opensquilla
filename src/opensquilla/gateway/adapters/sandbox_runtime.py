"""Gateway Adapter for the transport-neutral SandboxRuntime module."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from opensquilla.gateway.rpc import RpcContext

SandboxRead = Callable[[RpcContext], Awaitable[Mapping[str, Any]]]
SandboxCapabilityRead = Callable[[RpcContext, bool], Awaitable[Mapping[str, Any]]]
SandboxPolicyUpdate = Callable[[RpcContext, int, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]
SandboxModeWrite = Callable[[RpcContext, str], Awaitable[Mapping[str, Any]]]


class RpcContextSandboxRuntimePort:
    """Bind existing sandbox implementations to narrow application Ports."""

    def __init__(
        self,
        ctx: RpcContext,
        *,
        status: SandboxRead,
        setup_status: SandboxRead,
        ensure_setup: SandboxRead,
        capability: SandboxCapabilityRead,
        policy: SandboxRead,
        policy_defaults: SandboxRead,
        policy_update: SandboxPolicyUpdate,
        run_mode: SandboxRead,
        run_mode_write: SandboxModeWrite,
        runtime_status: SandboxRead,
    ) -> None:
        self.ctx = ctx
        self._status = status
        self._setup_status = setup_status
        self._ensure_setup = ensure_setup
        self._capability = capability
        self._policy = policy
        self._policy_defaults = policy_defaults
        self._policy_update = policy_update
        self._run_mode = run_mode
        self._run_mode_write = run_mode_write
        self._runtime_status = runtime_status

    async def read_status(self) -> Mapping[str, Any]: return await self._status(self.ctx)
    async def read_setup_status(self) -> Mapping[str, Any]:
        return await self._setup_status(self.ctx)
    async def ensure_setup(self) -> Mapping[str, Any]: return await self._ensure_setup(self.ctx)
    async def read_capability(self, *, refresh: bool) -> Mapping[str, Any]:
        return await self._capability(self.ctx, refresh)
    async def read_policy(self) -> Mapping[str, Any]: return await self._policy(self.ctx)
    async def read_policy_defaults(self) -> Mapping[str, Any]:
        return await self._policy_defaults(self.ctx)

    async def update_policy(
        self,
        base_policy_version: int,
        policy: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await self._policy_update(self.ctx, base_policy_version, policy)
    async def read_run_mode(self) -> Mapping[str, Any]: return await self._run_mode(self.ctx)
    async def write_run_mode(self, mode: str) -> Mapping[str, Any]:
        return await self._run_mode_write(self.ctx, mode)

    async def read_runtime_status(self) -> Mapping[str, Any]:
        return await self._runtime_status(self.ctx)


__all__ = ["RpcContextSandboxRuntimePort"]
