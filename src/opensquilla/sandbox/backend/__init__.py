"""Sandbox backend implementations and selection helper.

Four concrete backends ship today:

* :class:`~opensquilla.sandbox.backend.bubblewrap.BubblewrapBackend` - the Linux
  primary path; uses the ``bwrap`` binary for namespace isolation.
* :class:`~opensquilla.sandbox.backend.seatbelt.SeatbeltBackend` - macOS
  primary path; uses ``sandbox-exec`` with a generated SBPL profile.
* :class:`~opensquilla.sandbox.backend.windows_default.WindowsDefaultBackend`
  - native Windows path; prepares Windows sandbox grants and fails closed when
  policy enforcement is unavailable.
* :class:`~opensquilla.sandbox.backend.noop.NoopBackend` - used when the sandbox
  feature switch is off; runs the command through the existing rlimit wrapper
  and emits a warning on every invocation so the bypass is visible in logs.

:func:`select_backend` picks one based on the settings + host capabilities.
"""

from __future__ import annotations

import logging
import shutil
import sys

from opensquilla.sandbox.backend.base import Backend
from opensquilla.sandbox.backend.bubblewrap import BubblewrapBackend
from opensquilla.sandbox.backend.noop import NoopBackend
from opensquilla.sandbox.backend.seatbelt import SeatbeltBackend
from opensquilla.sandbox.backend.unavailable import UnavailableBackend
from opensquilla.sandbox.backend.windows_default import WindowsDefaultBackend
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.types import SandboxBackendError, SandboxSetupRequiredError

log = logging.getLogger(__name__)


def _auto_backend_failure_message() -> str:
    message = "sandbox=true but no real sandbox backend is available for backend=auto"
    if sys.platform.startswith("linux"):
        from opensquilla.sandbox.backend.linux_readiness import probe_bwrap

        probe = probe_bwrap()
        return f"{message}; Linux sandbox diagnostics: {probe.message}"
    if sys.platform == "darwin":
        from opensquilla.sandbox.backend.seatbelt import _sandbox_exec_binary

        sandbox_exec = _sandbox_exec_binary()
        diagnostics = (
            "macOS Seatbelt diagnostics: "
            f"sandbox-exec={'ready' if sandbox_exec is not None else 'missing'}"
        )
        return f"{message}; {diagnostics}"
    if not sys.platform.startswith("win"):
        return message

    from opensquilla.sandbox.backend.windows_default_support import (
        probe_windows_default_support,
    )

    support = probe_windows_default_support(proxy_ports=_windows_marker_proxy_ports())
    diagnostics = (
        "Windows sandbox setup diagnostics: "
        f"ctypes={'ready' if support.ctypes_available else 'missing'}, "
        f"windows_default={'ready' if support.default_backend_available else 'not ready'}, "
        f"network boundary={'ready' if support.proxy_allowlist_enforced else 'not ready'}"
    )
    return f"{message}; {diagnostics}"


def _windows_marker_proxy_ports() -> tuple[int, ...]:
    from opensquilla.sandbox.backend.windows_default_setup import (
        default_setup_marker_path,
        read_setup_marker,
    )

    marker = read_setup_marker(default_setup_marker_path())
    if marker is None or marker.network is None:
        return ()
    return marker.network.allowed_proxy_ports


def _backend_available(backend: Backend, *, verify_runtime: bool) -> bool:
    if verify_runtime:
        return backend.available()
    # Startup selects an installed backend, without test subprocesses, logons,
    # or write-open canaries. Real operations enforce and report their failures.
    if isinstance(backend, BubblewrapBackend):
        return sys.platform.startswith("linux") and shutil.which("bwrap") is not None
    if isinstance(backend, WindowsDefaultBackend):
        from opensquilla.sandbox.backend.windows_default_setup import default_setup_marker_path
        from opensquilla.sandbox.backend.windows_default_support import (
            probe_windows_default_support,
        )

        support = probe_windows_default_support(verify_runtime=False)
        if (
            support.ctypes_available
            and support.token_api_available
            and support.acl_api_available
            and not default_setup_marker_path().exists()
        ):
            raise SandboxSetupRequiredError("Windows sandbox has not been set up yet.")
        return support.default_backend_available and support.proxy_allowlist_enforced
    return backend.available()


def _auto_backend(*, verify_runtime: bool = True) -> Backend:
    """Pick the strongest available backend for the current host."""
    if sys.platform.startswith("linux"):
        bwrap = BubblewrapBackend()
        if _backend_available(bwrap, verify_runtime=verify_runtime):
            return bwrap
    if sys.platform == "darwin":
        seatbelt = SeatbeltBackend()
        if _backend_available(seatbelt, verify_runtime=verify_runtime):
            return seatbelt
    if sys.platform.startswith("win"):
        windows_default = WindowsDefaultBackend()
        if _backend_available(windows_default, verify_runtime=verify_runtime):
            return windows_default
    return NoopBackend()


def select_backend(settings: SandboxSettings, *, verify_runtime: bool = True) -> Backend:
    """Return the backend matching ``settings.backend``.

    ``"auto"`` defers to :func:`_auto_backend`. Explicit choices are honoured
    even when the backend is unavailable - the caller will see an honest
    ``available() is False`` and can decide whether to degrade or abort.
    Selection is logged so operators can correlate runtime behaviour with
    config.
    """
    choice = settings.backend
    backend: Backend
    if not settings.sandbox:
        backend = NoopBackend()
    elif choice == "auto":
        backend = _auto_backend(verify_runtime=verify_runtime)
    elif choice == "bubblewrap":
        backend = BubblewrapBackend()
    elif choice == "seatbelt":
        backend = SeatbeltBackend()
    elif choice == "noop":
        backend = NoopBackend()
    elif choice == "windows_default":
        backend = WindowsDefaultBackend()
    else:  # pragma: no cover - pydantic Literal constrains this upstream
        raise ValueError(f"unknown sandbox backend: {choice!r}")

    available = _backend_available(backend, verify_runtime=verify_runtime)
    log.info(
        "sandbox.backend_selected: choice=%s resolved=%s available=%s",
        choice,
        backend.name,
        available,
    )
    if settings.sandbox and choice == "auto" and isinstance(backend, NoopBackend):
        if not verify_runtime:
            raise SandboxBackendError(
                "No installed sandbox backend with completed setup is available."
            )
        raise SandboxBackendError(_auto_backend_failure_message())
    if settings.sandbox and choice != "noop" and not available:
        raise SandboxBackendError(
            f"sandbox backend {backend.name!r} is unavailable while sandbox=true"
        )
    return backend


__all__ = [
    "Backend",
    "BubblewrapBackend",
    "NoopBackend",
    "SeatbeltBackend",
    "UnavailableBackend",
    "WindowsDefaultBackend",
    "select_backend",
]
