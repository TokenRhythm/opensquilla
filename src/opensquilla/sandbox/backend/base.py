"""Backend abstract interface.

A backend is stateless: each :meth:`Backend.run` call materialises its own
sandbox (per-command ephemeral lifecycle) and releases it at completion or
cancellation. Backends must surface setup failures by raising
:class:`~opensquilla.sandbox.types.SandboxBackendError`; they must never fall
back to unsandboxed host execution on failure.

``available`` remains a cheap selection check. ``probe_runtime`` is the
bounded startup check used after selection when callers need evidence that the
actual helper launch path works. Capability status reads do not call it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from opensquilla.sandbox.types import SandboxBackendError, SandboxRequest, SandboxResult

if TYPE_CHECKING:
    from opensquilla.sandbox.operation_runtime import (
        SandboxOperation,
        SandboxOperationDomain,
        SandboxOperationResult,
    )


class Backend(ABC):
    """Abstract base every sandbox backend implements."""

    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """Return ``True`` when this backend can run on the current host.

        Must be cheap (no subprocesses, no filesystem probing beyond a
        ``which`` lookup). Callers invoke this during startup to decide
        which backend to promote.
        """

    @abstractmethod
    async def run(self, request: SandboxRequest) -> SandboxResult:
        """Execute ``request`` and return a :class:`SandboxResult`.

        Implementations must honour the wall timeout in ``request.policy``
        and surface timeouts via :attr:`SandboxResult.timed_out`. Setup
        failures raise :class:`SandboxBackendError`; non-zero exit codes do
        not.
        """

    async def probe_runtime(self, *, cwd: Path | None = None) -> None:
        """Validate the selected backend without running user work.

        Backends with a meaningful no-op launch path override this method. The
        default keeps test and third-party backends source-compatible while
        still failing closed when their cheap availability check is negative.
        """

        _ = cwd
        if not self.available():
            raise SandboxBackendError(
                f"helper_probe_failed: {self.name} backend is unavailable"
            )

    def operation_domains_supported(self) -> frozenset[SandboxOperationDomain]:
        """Return operation domains this backend can run through the sandbox."""

        return frozenset()

    async def run_operation(
        self,
        operation: SandboxOperation,
    ) -> SandboxOperationResult:
        """Run a non-process operation under this backend."""

        raise NotImplementedError(
            f"{operation.domain} operations are not supported by this backend"
        )


__all__ = ["Backend"]
