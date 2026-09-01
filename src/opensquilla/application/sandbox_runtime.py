"""Transport-neutral SandboxRuntime application module.

The Gateway still owns the concrete sandbox implementation.  This module
owns the use-case boundary and validation so UI adapters never need to know
which sandbox package, policy store, or runtime-pack implementation is used.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class SandboxRuntimePort(Protocol):
    async def read_status(self) -> Mapping[str, Any]: ...

    async def read_setup_status(self) -> Mapping[str, Any]: ...

    async def ensure_setup(self) -> Mapping[str, Any]: ...

    async def read_capability(self, *, refresh: bool) -> Mapping[str, Any]: ...

    async def read_policy(self) -> Mapping[str, Any]: ...

    async def read_policy_defaults(self) -> Mapping[str, Any]: ...

    async def update_policy(
        self,
        base_policy_version: int,
        policy: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def read_run_mode(self) -> Mapping[str, Any]: ...

    async def write_run_mode(self, mode: str) -> Mapping[str, Any]: ...

    async def read_runtime_status(self) -> Mapping[str, Any]: ...


class SandboxRuntime:
    """Deep domain seam for sandbox posture, setup, policy and runtime reads."""

    def __init__(self, port: SandboxRuntimePort) -> None:
        self._port = port

    async def status(self) -> dict[str, Any]:
        return dict(await self._port.read_status())

    async def setup_status(self) -> dict[str, Any]:
        return dict(await self._port.read_setup_status())

    async def ensure_setup(self) -> dict[str, Any]:
        return dict(await self._port.ensure_setup())

    async def capability(self, *, refresh: bool = False) -> dict[str, Any]:
        return dict(await self._port.read_capability(refresh=bool(refresh)))

    async def policy(self) -> dict[str, Any]:
        return dict(await self._port.read_policy())

    async def policy_defaults(self) -> dict[str, Any]:
        return dict(await self._port.read_policy_defaults())

    async def update_policy(
        self,
        base_policy_version: int,
        policy: Mapping[str, Any],
    ) -> dict[str, Any]:
        if isinstance(base_policy_version, bool) or not isinstance(base_policy_version, int):
            raise ValueError("base policy version must be an integer")
        if not isinstance(policy, Mapping):
            raise ValueError("sandbox policy must be an object")
        return dict(await self._port.update_policy(base_policy_version, dict(policy)))

    async def run_mode(self) -> dict[str, Any]:
        return dict(await self._port.read_run_mode())

    async def set_run_mode(self, mode: str) -> dict[str, Any]:
        normalized = str(mode or "").strip()
        if normalized not in {"safe", "full"}:
            raise ValueError("sandbox run mode must be safe or full")
        return dict(await self._port.write_run_mode(normalized))

    async def runtime_status(self) -> dict[str, Any]:
        return dict(await self._port.read_runtime_status())


class InMemorySandboxRuntimePort:
    """Deterministic Port implementation for application-level tests."""

    def __init__(
        self,
        *,
        status: Mapping[str, Any] | None = None,
        setup_status: Mapping[str, Any] | None = None,
        capability: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        policy_defaults: Mapping[str, Any] | None = None,
        run_mode: str = "full",
        runtime_status: Mapping[str, Any] | None = None,
    ) -> None:
        self.status_value = dict(status or {})
        self.setup_status_value = dict(setup_status or {})
        self.capability_value = dict(capability or {})
        self.policy_value = dict(policy or {})
        self.policy_defaults_value = dict(policy_defaults or {})
        self.run_mode_value = run_mode
        self.runtime_status_value = dict(runtime_status or {})

    async def read_status(self) -> Mapping[str, Any]: return self.status_value
    async def read_setup_status(self) -> Mapping[str, Any]: return self.setup_status_value
    async def ensure_setup(self) -> Mapping[str, Any]: return self.setup_status_value
    async def read_capability(self, *, refresh: bool) -> Mapping[str, Any]:
        return self.capability_value
    async def read_policy(self) -> Mapping[str, Any]: return self.policy_value
    async def read_policy_defaults(self) -> Mapping[str, Any]: return self.policy_defaults_value
    async def update_policy(
        self,
        base_policy_version: int,
        policy: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.policy_value = dict(policy)
        return self.policy_value
    async def read_run_mode(self) -> Mapping[str, Any]:
        return {"runMode": self.run_mode_value, "source": "preference"}
    async def write_run_mode(self, mode: str) -> Mapping[str, Any]:
        self.run_mode_value = mode
        return {"runMode": mode, "source": "preference"}
    async def read_runtime_status(self) -> Mapping[str, Any]: return self.runtime_status_value


__all__ = ["InMemorySandboxRuntimePort", "SandboxRuntime", "SandboxRuntimePort"]
