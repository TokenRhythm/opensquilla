"""Runtime facade for the sandbox subsystem.

This module owns the *process-wide* glue between:

* :class:`~opensquilla.sandbox.config.SandboxSettings` — operator configuration
* :class:`~opensquilla.sandbox.governance.ApprovalGate` — human approval bridge
* :class:`~opensquilla.sandbox.governance.DenialLedger` — §8.5 denial bookkeeping
* :class:`~opensquilla.sandbox.stale_output_cache.StaleOutputCache` — §8.3 hygiene
* :class:`~opensquilla.sandbox.backend.Backend` — the concrete isolation layer

The rest of the code base talks to the sandbox through three entry points:

* :func:`configure_runtime` — called exactly once during gateway boot.
* :func:`get_runtime` — cheap accessor for tool handlers.
* :func:`sandboxed` — a decorator factory for async tool handlers that
  threads the governance gate and (optionally) a real backend execution.

The decorator is intentionally conservative: it consults the gate with the
resolved policy and denies with a structured envelope before the wrapped
handler runs. Whether the handler then also delegates to a sandbox backend
for the actual command is an orthogonal decision — the filesystem tools run
in-process after the gate, while the shell tools additionally spawn through
:meth:`Backend.run`.

Nothing in this module performs isolation by itself; it routes to whichever
backend :func:`opensquilla.sandbox.backend.select_backend` picked for the current
host.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import functools
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from opensquilla.sandbox.backend import Backend, NoopBackend, UnavailableBackend, select_backend
from opensquilla.sandbox.capability_profile import capability_profile_for_command
from opensquilla.sandbox.config import EffectiveMode, SandboxSettings
from opensquilla.sandbox.elevation import (
    ApprovalReviewerName,
    ElevationAction,
    ElevationGateResult,
    consume_approved_elevation,
    gate_elevated_action,
)
from opensquilla.sandbox.escalation import (
    build_network_approval_params,
    build_package_bundle_approval_params,
    context_with_temporary_network_grants,
    current_tool_run_context,
    remember_resolved_run_context,
    request_sandbox_approval,
    reset_resolved_run_context_overlays,
)
from opensquilla.sandbox.governance import (
    ApprovalGate,
    DenialLedger,
    action_fingerprint,
    gate_execution,
    on_successful_exec,
)
from opensquilla.sandbox.managed_proxy_env import (
    managed_proxy_env,
    managed_proxy_env_names_upper,
)
from opensquilla.sandbox.network_guard import NetworkDecision, decide_network_access
from opensquilla.sandbox.network_proxy import SandboxProxyServer
from opensquilla.sandbox.network_runtime import NetworkApprovalService
from opensquilla.sandbox.operation_runtime import SandboxOperation, SandboxOperationRuntime
from opensquilla.sandbox.path_validation import (
    decide_path_access,
    normalize_mount_access,
    normalize_path,
)
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.sandbox.policy import LevelHints, build_policy, select_level
from opensquilla.sandbox.run_context import DomainGrant, PackageBundleGrant, RunContext
from opensquilla.sandbox.run_context_service import auto_add_trusted_domain_grant
from opensquilla.sandbox.run_mode import RunMode, normalize_run_mode
from opensquilla.sandbox.stale_output_cache import StaleOutputCache, get_stale_output_cache
from opensquilla.sandbox.types import (
    ApprovalDecision,
    DenialReason,
    DenialResult,
    FollowupTag,
    MountSpec,
    NetworkMode,
    NetworkProxySpec,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
    SecurityLevel,
    SuggestedNextStep,
)

log = logging.getLogger(__name__)

_MANAGED_NETWORK_PROXY_URL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "opensquilla_managed_network_proxy_url",
    default=None,
)
_MANAGED_PROXY_ENV_NAMES_UPPER = managed_proxy_env_names_upper(
    include_windows_git=True,
)

_IN_PROCESS_NETWORK_TAGS: frozenset[str] = frozenset(
    {"network.fetch", "network.http", "web.fetch"}
)
_SEARCH_PROVIDER_SYSTEM_DOMAINS: dict[str, tuple[str, ...]] = {
    "bocha": ("api.bochaai.com",),
    "brave": ("api.search.brave.com",),
    "duckduckgo": ("html.duckduckgo.com",),
    "exa": ("api.exa.ai",),
    "iqs": ("cloud-iqs.aliyuncs.com",),
    "tavily": ("api.tavily.com",),
}


# ─── Approval queue / context protocols ──────────────────────────────────


class _ApprovalQueueLike(Protocol):
    """Structural subset of :class:`opensquilla.gateway.approval_queue.ApprovalQueue`."""

    def request(self, namespace: str = ..., params: dict | None = ...) -> str: ...

    async def wait(self, approval_id: str, timeout: float | None = ...) -> bool: ...

    def resolve(self, approval_id: str, approved: bool) -> None: ...


# ─── Runtime state ────────────────────────────────────────────────────────


@dataclass
class SandboxRuntime:
    """Process-wide sandbox runtime assembled from settings.

    Callers either pass it around explicitly (tests) or fetch it via
    :func:`get_runtime`. The backend may be atomically promoted after a
    successful platform setup; the gate, ledger, cache, queue, and settings
    remain stable for the lifetime of the runtime.
    """

    settings: SandboxSettings
    effective: EffectiveMode
    backend: Backend
    gate: ApprovalGate
    ledger: DenialLedger
    cache: StaleOutputCache
    workspace: Path
    approval_queue: Any


@dataclass(frozen=True)
class ManagedNetworkSubprocess:
    """A subprocess request plus cleanup for a live managed-network proxy."""

    request: SandboxRequest
    cleanup: Callable[[], Awaitable[None]]


_runtime: SandboxRuntime | None = None
_WINDOWS_FIXED_PROXY_PORT_LOCK: asyncio.Lock | None = None


def configure_runtime(
    settings: SandboxSettings,
    *,
    approval_queue: _ApprovalQueueLike | None = None,
    stale_cache: StaleOutputCache | None = None,
    workspace: Path | None = None,
) -> SandboxRuntime:
    """Build the process-wide :class:`SandboxRuntime`.

    Called exactly once from :func:`opensquilla.gateway.boot.build_services` after
    :meth:`SandboxSettings.validate_combination` has emitted its log line.
    Tests may call it repeatedly; each call replaces the prior runtime.
    """
    global _runtime

    effective = settings.validate_combination()
    cache = stale_cache if stale_cache is not None else get_stale_output_cache()
    ledger = DenialLedger(
        threshold=max(1, settings.denial_threshold),
        stale_output_cache=cache,
    )
    backend: Backend
    if not effective.sandbox_enabled:
        backend = NoopBackend()
    else:
        try:
            backend = select_backend(settings)
        except SandboxBackendError as exc:
            if settings.backend != "auto":
                raise
            backend = UnavailableBackend(str(exc))
            log.warning(
                "sandbox.backend_unavailable: backend=auto reason=%s; "
                "Managed Execution will request an exact reviewed host retry; "
                "Standard mode remains fail closed",
                exc,
            )
        if backend.name == "noop" and settings.backend != "noop":
            raise SandboxBackendError(
                "sandbox=true requires a real backend; refusing implicit noop fallback"
            )

    if approval_queue is not None:
        queue = approval_queue
        gate = ApprovalGate(queue)
    else:
        # Lazy import: avoids a circular import when gateway is not yet loaded.
        from opensquilla.gateway.approval_queue import get_approval_queue

        queue = get_approval_queue()
        gate = ApprovalGate(queue)

    ws = workspace if workspace is not None else Path.cwd()
    _runtime = SandboxRuntime(
        settings=settings,
        effective=effective,
        backend=backend,
        gate=gate,
        ledger=ledger,
        cache=cache,
        workspace=ws,
        approval_queue=queue,
    )
    log.info(
        "sandbox.runtime_configured: backend=%s level=%s grading=%s insecure=%s",
        backend.name,
        effective.default_level.label,
        effective.grading_enabled,
        effective.insecure_mode,
    )
    return _runtime


def get_runtime() -> SandboxRuntime | None:
    """Return the configured runtime or ``None`` when unconfigured.

    ``None`` is *not* an implicit opt-out: :func:`gate_action` fails closed
    (``DenialReason.RUNTIME_
    UNCONFIGURED``) whenever the runtime is missing. Callers that genuinely
    want sandbox-off behaviour in tests / CLI one-shots must call
    :func:`configure_runtime` with ``SandboxSettings(sandbox=False)`` rather
    than relying on the ``None`` branch.
    """
    return _runtime


def refresh_runtime_backend_after_setup() -> Backend | None:
    """Promote an auto-configured unavailable backend after platform setup.

    Runtime setup happens after gateway construction on Windows. Replacing
    the whole runtime would discard approval and denial state, so this
    function changes only the backend reference. Existing real backends and
    sandbox-disabled runtimes are deliberately left untouched.
    """

    runtime = _runtime
    if runtime is None:
        return None
    if not runtime.effective.sandbox_enabled:
        return runtime.backend
    if not isinstance(runtime.backend, UnavailableBackend):
        return runtime.backend

    backend = select_backend(runtime.settings)
    if isinstance(backend, (NoopBackend, UnavailableBackend)):
        raise SandboxBackendError(
            "sandbox setup completed but no real sandbox backend became available"
        )
    runtime.backend = backend
    log.info("sandbox.runtime_backend_promoted: backend=%s", backend.name)
    return backend


def active_file_system_profile(
    workspace: Path | None = None,
) -> FileSystemPermissionProfile | None:
    """Return the canonical default profile used by sandboxed direct tools.

    In-process filesystem tools cannot rely on a backend mount namespace to
    enforce the policy.  They must resolve paths against the same profile that
    a standard sandboxed subprocess receives.
    """

    from opensquilla.tools.types import current_tool_context

    tool_context = current_tool_context.get()
    override = (
        tool_context.sandbox_file_system_profile
        if tool_context is not None
        else None
    )
    if isinstance(override, FileSystemPermissionProfile):
        return override

    runtime = get_runtime()
    if runtime is None or not runtime.effective.sandbox_enabled:
        return None
    effective_workspace = (
        workspace.expanduser().resolve(strict=False)
        if workspace is not None
        else _resolve_workspace(runtime, None).expanduser().resolve(strict=False)
    )
    policy = build_policy(
        SecurityLevel.STANDARD,
        "fs.write",
        effective_workspace,
        runtime.settings,
        session_mounts=_session_mounts_for_policy(effective_workspace),
    )
    return policy.file_system


def reset_runtime() -> None:
    """Drop the process-wide runtime. Test helper."""
    global _runtime
    _runtime = None
    reset_resolved_run_context_overlays()


# ─── Core helpers ─────────────────────────────────────────────────────────


def _default_argv(action_kind: str, arguments: dict[str, Any]) -> tuple[str, ...]:
    """Derive a stable argv-like tuple from tool kwargs for fingerprinting.

    We avoid guessing: the caller can pass an explicit ``argv_factory`` to
    :func:`sandboxed`. When nothing is supplied we fall back to a simple
    serialisation of the arguments so the fingerprint is still deterministic
    per call site.
    """
    if "command" in arguments and isinstance(arguments["command"], str):
        return (action_kind, arguments["command"])
    if "argv" in arguments and isinstance(arguments["argv"], (list, tuple)):
        return (action_kind, *(str(x) for x in arguments["argv"]))
    payload = json.dumps({k: _stringify(v) for k, v in sorted(arguments.items())})
    return (action_kind, payload)


def _stringify(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_stringify(x) for x in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}={_stringify(v)}" for k, v in sorted(value.items())) + "}"
    return type(value).__name__


def _resolve_session_id(runtime: SandboxRuntime, session_id: str | None) -> str:
    if session_id:
        return session_id
    try:
        from opensquilla.tools.types import current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        ctx = None
    if ctx is not None and getattr(ctx, "session_key", None):
        return str(ctx.session_key)
    return "default"


def _resolve_workspace(runtime: SandboxRuntime, cwd: str | None) -> Path:
    if cwd:
        p = Path(cwd)
        if p.is_absolute():
            return p
    try:
        from opensquilla.tools.types import current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        ctx = None
    workspace_dir = getattr(ctx, "workspace_dir", None) if ctx is not None else None
    if isinstance(workspace_dir, str) and workspace_dir:
        wp = Path(workspace_dir)
        if wp.is_absolute():
            return wp
    if runtime.workspace.is_absolute():
        return runtime.workspace
    return Path.cwd()


def _resolve_request_run_mode(runtime: SandboxRuntime | None) -> str:
    try:
        from opensquilla.tools.run_mode import current_run_mode

        mode = current_run_mode()
        if mode is not None:
            return mode
    except Exception:  # pragma: no cover - defensive against tool-context imports
        pass
    context = current_tool_run_context()
    if isinstance(context, RunContext):
        return context.run_mode.value
    if runtime is not None:
        return normalize_run_mode(runtime.settings.run_mode).value
    return RunMode.FULL.value


def _session_mounts_for_policy(workspace: Path) -> tuple[MountSpec, ...]:
    try:
        from opensquilla.tools.types import current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        ctx = None

    run_context = current_tool_run_context()
    source_items: list[dict[str, Any]]
    if isinstance(run_context, RunContext):
        source_items = [
            {
                "path": grant.path,
                "access": grant.access,
            }
            for grant in run_context.mounts
        ]
    else:
        source_items = []
        raw_mounts = getattr(ctx, "sandbox_mounts", None) if ctx is not None else None
        if isinstance(raw_mounts, list):
            for item in raw_mounts:
                if not isinstance(item, dict):
                    continue
                raw_path = item.get("path")
                if not isinstance(raw_path, str) or not raw_path.strip():
                    continue
                source_items.append(
                    {
                        "path": raw_path,
                        "access": item.get("access"),
                    }
                )

    merged_items: dict[str, dict[str, Any]] = {}
    for item in source_items:
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        merged_items[raw_path] = item

    if not merged_items:
        return ()

    mounts: list[MountSpec] = []
    for item in merged_items.values():
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        access = normalize_mount_access(item.get("access"))
        try:
            host_path = normalize_path(raw_path)
            if not host_path.exists():
                continue
            decision = decide_path_access(
                host_path,
                workspace=workspace,
                mounts=(),
                write=access == "rw",
            )
        except (OSError, RuntimeError):
            continue
        if decision.status == "blocked":
            continue
        mounts.append(
            MountSpec(
                host_path=host_path,
                sandbox_path=host_path,
                mode=access,
                required=False,
            )
        )
    return tuple(mounts)


def build_request(
    *,
    action_kind: str,
    argv: tuple[str, ...],
    cwd: Path,
    policy: SandboxPolicy,
    env: dict[str, str] | None = None,
    reason: str = "",
) -> SandboxRequest:
    """Assemble a :class:`SandboxRequest` for the current action.

    Exposed for callers (notably shell.py) that want to fingerprint a
    command without going through the decorator.
    """
    runtime = get_runtime()
    session_id = _resolve_session_id(runtime, None) if runtime is not None else ""
    return SandboxRequest(
        argv=argv,
        cwd=cwd,
        action_kind=action_kind,
        policy=policy,
        env=dict(env or {}),
        reason=reason,
        session_id=session_id,
        run_mode=_resolve_request_run_mode(runtime),
    )


def _backend_name(runtime: SandboxRuntime | object | None) -> str:
    backend = getattr(runtime, "backend", None) if runtime is not None else None
    name = getattr(backend, "name", "")
    return str(name or "")


def _windows_proxy_allowlist_enforced(
    runtime: SandboxRuntime | object | None,
    *,
    proxy_ports: tuple[int, ...] = (),
) -> bool:
    backend_name = _backend_name(runtime).lower()
    if not backend_name.startswith("windows_"):
        return True
    try:
        from opensquilla.sandbox.backend.windows_default_support import (
            probe_windows_default_support,
        )
    except Exception:
        return False
    if not proxy_ports:
        proxy_ports = _windows_allowed_proxy_ports(runtime)
    return bool(
        probe_windows_default_support(proxy_ports=proxy_ports).proxy_allowlist_enforced
    )


def _windows_allowed_proxy_ports(
    runtime: SandboxRuntime | object | None,
) -> tuple[int, ...]:
    backend_name = _backend_name(runtime).lower()
    if not backend_name.startswith("windows_"):
        return ()
    try:
        from opensquilla.sandbox.backend.windows_default_setup import (
            default_setup_marker_path,
            read_setup_marker,
        )
    except Exception:
        return ()
    marker = read_setup_marker(default_setup_marker_path())
    if marker is None or marker.network is None:
        return ()
    return marker.network.allowed_proxy_ports


def _windows_proxy_allowlist_unavailable_detail(
    runtime: SandboxRuntime | object | None = None,
) -> str | None:
    if not _backend_name(runtime).lower().startswith("windows_"):
        return None
    try:
        from opensquilla.sandbox.backend.windows_default_network import (
            FIREWALL_RULE_VERSION,
            NETWORK_SETUP_VERSION,
            WFP_RULE_VERSION,
        )
        from opensquilla.sandbox.backend.windows_default_setup import (
            default_setup_marker_path,
            read_setup_marker,
        )
    except Exception:
        return None
    marker = read_setup_marker(default_setup_marker_path())
    if marker is None:
        return "setup marker is missing"
    if marker.network is None:
        return "network marker is missing"
    network = marker.network
    parts: list[str] = []
    if network.firewall_rule_version != FIREWALL_RULE_VERSION:
        parts.append(
            f"firewall={network.firewall_rule_version} required={FIREWALL_RULE_VERSION}"
        )
    if network.wfp_rule_version != WFP_RULE_VERSION:
        parts.append(f"wfp={network.wfp_rule_version} required={WFP_RULE_VERSION}")
    if network.network_setup_version != NETWORK_SETUP_VERSION:
        parts.append(
            "network_setup="
            f"{network.network_setup_version} required={NETWORK_SETUP_VERSION}"
        )
    proxy_ports = _windows_allowed_proxy_ports(runtime)
    if proxy_ports and network.allowed_proxy_ports != tuple(sorted(set(proxy_ports))):
        parts.append(f"ports={network.allowed_proxy_ports} required={proxy_ports}")
    if parts:
        return "network marker is out of date: " + ", ".join(parts)
    return "network marker is not enforced by Windows"


def _windows_managed_network_unavailable_message(
    runtime: SandboxRuntime | object | None = None,
) -> str:
    message = (
        "Windows sandbox managed network is unavailable: PROXY_ALLOWLIST "
        "is not enforced by the active Windows backend. Do not retry "
        "with http_request. Do not retry with web_fetch. Do not retry "
        "with offline wheel downloads. Do not retry with host Python. "
        "Keep the operation blocked until the Windows proxy allowlist "
        "backend is enabled or the user changes run mode."
    )
    detail = _windows_proxy_allowlist_unavailable_detail(runtime)
    if detail:
        message = f"{message} Detail: {detail}."
    return message


def _windows_process_is_admin() -> bool:
    try:
        import ctypes

        windll = cast(Any, ctypes).windll
        return bool(windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


async def _ensure_windows_proxy_allowlist_setup(
    runtime: SandboxRuntime | object | None,
) -> bool:
    if not _backend_name(runtime).lower().startswith("windows_"):
        return True
    if not _windows_process_is_admin():
        return False
    try:
        from opensquilla.sandbox.setup_state import (
            SandboxSetupState,
            ensure_sandbox_setup,
        )
    except Exception:
        return False
    try:
        result = await ensure_sandbox_setup(getattr(runtime, "settings", None))
    except Exception:
        return False
    return getattr(result, "state", None) is SandboxSetupState.READY


async def _windows_proxy_allowlist_ready_or_repaired(
    runtime: SandboxRuntime | object | None,
) -> bool:
    if _windows_proxy_allowlist_enforced(runtime):
        return True
    if not _windows_process_is_admin():
        return False
    return await _ensure_windows_proxy_allowlist_setup(runtime)


async def _platform_proxy_allowlist_ready_or_repaired(
    runtime: SandboxRuntime | object | None,
) -> bool:
    if not _backend_name(runtime).lower().startswith("windows_"):
        return True
    return await _windows_proxy_allowlist_ready_or_repaired(runtime)


def _managed_proxy_env(
    *,
    backend_name: str | None,
    host: str,
    port: int,
) -> dict[str, str]:
    if str(backend_name or "").lower().startswith("windows_"):
        return managed_proxy_env(host, port, windows_git_ssl_backend=True)
    return managed_proxy_env(host, port)


def request_with_managed_network_proxy_env(
    request: SandboxRequest,
    *,
    backend_name: str | None = None,
) -> SandboxRequest:
    """Return ``request`` with managed proxy environment wired for subprocesses."""
    if (
        request.policy.network != NetworkMode.PROXY_ALLOWLIST
        or request.policy.network_proxy is None
    ):
        return request

    proxy = request.policy.network_proxy
    proxy_env = _managed_proxy_env(
        backend_name=backend_name,
        host=proxy.host,
        port=proxy.port,
    )
    managed_env_keys = tuple(proxy_env)
    env = {
        key: value
        for key, value in request.env.items()
        if key.upper() not in _MANAGED_PROXY_ENV_NAMES_UPPER
    }
    env.update(proxy_env)

    env_allowlist = tuple(
        dict.fromkeys(
            (
                *(
                    key
                        for key in request.policy.env_allowlist
                        if key.upper() not in _MANAGED_PROXY_ENV_NAMES_UPPER
                ),
                *managed_env_keys,
            )
        )
    )
    policy = request.policy
    if env_allowlist != request.policy.env_allowlist:
        policy = dataclasses.replace(request.policy, env_allowlist=env_allowlist)

    return SandboxRequest(
        argv=request.argv,
        cwd=request.cwd,
        action_kind=request.action_kind,
        policy=policy,
        stdin=request.stdin,
        env=env,
        reason=request.reason,
        session_id=request.session_id,
        run_mode=request.run_mode,
    )


async def gate_action(
    *,
    action_kind: str,
    argv: tuple[str, ...],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    followup_tag: FollowupTag = FollowupTag.NONE,
    hints: LevelHints | None = None,
    session_id: str | None = None,
    reason: str = "",
    runtime: SandboxRuntime | None = None,
) -> tuple[ApprovalDecision, SandboxPolicy, SandboxRequest]:
    """Consult the approval gate for an action.

    Returns a triple ``(decision, policy, request)``. The ``request`` and
    ``policy`` are always populated even on denial so callers can log
    action fingerprints and levels uniformly.
    """
    rt = runtime or get_runtime()
    if rt is None:
        # Fail-closed: a side-effecting tool reached the sandbox gate before
        # ``configure_runtime()`` ran. Silently allowing would turn a boot
        # order bug into unsandboxed host execution. Callers that genuinely
        # want sandbox off must pass an explicit ``SandboxSettings(sandbox=
        # False)`` runtime (via :func:`configure_runtime`) rather than
        # relying on ``None``.
        ws = Path(cwd) if cwd and Path(cwd).is_absolute() else Path.cwd()
        settings = SandboxSettings(sandbox=False, security_grading=False)
        policy = build_policy(
            SecurityLevel.STANDARD,
            action_kind,
            ws,
            settings,
            trusted=True,
            hints=hints,
        )
        req = build_request(
            action_kind=action_kind,
            argv=argv,
            cwd=ws,
            policy=policy,
            env=env,
            reason=reason,
        )
        from opensquilla.sandbox.governance import action_fingerprint

        log.warning(
            "sandbox.runtime_unconfigured: action_kind=%s — denying fail-closed",
            action_kind,
        )
        denial = DenialResult(
            reason=DenialReason.RUNTIME_UNCONFIGURED,
            suggested_next_step=SuggestedNextStep.ASK_USER,
            level=policy.level,
            action_fingerprint=action_fingerprint(req),
            message=(
                "Sandbox runtime is not configured. Side-effecting tools "
                "refuse to run until configure_runtime() has been called. "
                "This is a fail-closed guard; do not retry without fixing "
                "the boot order."
            ),
            retryable=False,
        )
        return denial, policy, req

    workspace = _resolve_workspace(rt, str(cwd) if cwd else None)
    level = (
        select_level(action_kind, hints)
        if rt.effective.grading_enabled
        else rt.effective.default_level
    )
    policy = build_policy(
        level,
        action_kind,
        workspace,
        rt.settings,
        trusted=(hints is None or hints.trusted_source),
        hints=hints,
        session_mounts=_session_mounts_for_policy(workspace),
    )
    run_context = current_tool_run_context()
    configured_mode = getattr(rt.settings, "run_mode", None)
    try:
        from opensquilla.tools.run_mode import current_run_mode

        active_mode = current_run_mode()
    except Exception:  # pragma: no cover - defensive against tool-context cycles
        active_mode = None
    managed_execution = bool(
        active_mode == RunMode.TRUSTED.value
        or (run_context is not None and run_context.run_mode == RunMode.TRUSTED)
        or (
            active_mode is None
            and run_context is None
            and configured_mode is not None
            and normalize_run_mode(configured_mode) == RunMode.TRUSTED
        )
    )
    if managed_execution and policy.require_approval:
        # Managed Execution reviews only an exact host escape. Waiting on the
        # legacy policy approval gate here can block a sandboxed tool for five
        # minutes before it ever reaches the deterministic elevation rules.
        policy = dataclasses.replace(policy, require_approval=False)
    request = build_request(
        action_kind=action_kind,
        argv=argv,
        cwd=workspace,
        policy=policy,
        env=env,
        reason=reason,
    )
    decision = await gate_execution(
        request,
        policy,
        session_id=_resolve_session_id(rt, session_id),
        ledger=rt.ledger,
        approval_gate=rt.gate,
        followup_tag=followup_tag,
    )
    return decision, policy, request


async def run_under_backend(
    request: SandboxRequest,
    *,
    runtime: SandboxRuntime | None = None,
) -> SandboxResult:
    """Dispatch ``request`` through the configured backend.

    The gate must already have returned :data:`ALLOW` before this is called.
    A missing runtime is a boot-order or caller-contract bug; callers that
    need noop behavior must configure an explicit runtime with ``backend="noop"``.
    """
    rt = runtime or get_runtime()
    if rt is None:
        raise SandboxBackendError(
            "Sandbox runtime is not configured; refusing to run backend request"
        )
    if (
        request.policy.network == NetworkMode.PROXY_ALLOWLIST
        and request.policy.network_proxy is None
    ):
        return await _run_with_managed_network_proxy(request, rt)
    request = request_with_managed_network_proxy_env(
        request,
        backend_name=_backend_name(rt),
    )
    return await _run_backend_with_platform_network_boundary(request, rt)


async def _noop_managed_network_cleanup() -> None:
    return None


def _current_run_context_for_network_proxy() -> RunContext | None:
    return current_tool_run_context()


def _network_grant_workspace(request: SandboxRequest, runtime: SandboxRuntime) -> str:
    try:
        from opensquilla.tools.types import current_tool_context

        ctx = current_tool_context.get()
    except Exception:  # pragma: no cover - defensive
        ctx = None
    if ctx is not None:
        workspace = str(getattr(ctx, "workspace_dir", None) or "").strip()
        if workspace:
            return workspace
        run_context = getattr(ctx, "sandbox_run_context", None)
        if isinstance(run_context, RunContext) and run_context.workspace:
            return run_context.workspace

    context = _current_run_context_for_network_proxy()
    if context is not None and context.workspace:
        return context.workspace
    return str(getattr(runtime, "workspace", None) or request.cwd)


def _configured_approval_reviewer(runtime: SandboxRuntime | object) -> ApprovalReviewerName:
    settings = getattr(runtime, "settings", None)
    reviewer = str(getattr(settings, "approvals_reviewer", "user") or "user")
    return cast(
        "ApprovalReviewerName",
        reviewer if reviewer in {"user", "auto_review"} else "user",
    )


def _current_sandbox_persistence_handles() -> tuple[Any | None, Any | None]:
    try:
        from opensquilla.tools.builtin import sessions as sessions_mod
    except Exception:  # pragma: no cover - defensive
        return None, None
    return getattr(sessions_mod, "_session_manager", None), getattr(
        sessions_mod,
        "_gateway_config",
        None,
    )


async def _persist_auto_trusted_host_if_available(
    request: SandboxRequest,
    runtime: SandboxRuntime,
    *,
    decision: NetworkDecision,
) -> None:
    if decision.reason != "auto_trusted" or decision.source != "auto_trusted:chat":
        return
    session_key = _resolve_session_id(runtime, None)
    if not session_key:
        return
    session_manager, config = _current_sandbox_persistence_handles()
    if session_manager is None or config is None:
        return
    workspace = _network_grant_workspace(request, runtime)
    try:
        context = await auto_add_trusted_domain_grant(
            session_manager,
            session_key,
            domain=decision.normalized_host,
            config=config,
            workspace=workspace,
        )
    except Exception:
        return
    remember_resolved_run_context(
        session_key,
        workspace,
        context,
        session_manager=session_manager,
        config=config,
    )


def _auto_trusted_persistence_callback(
    request: SandboxRequest,
    runtime: SandboxRuntime,
    *,
    context: RunContext,
) -> Callable[[NetworkDecision], Awaitable[None]] | None:
    if context.run_mode != RunMode.TRUSTED:
        return None
    session_manager, config = _current_sandbox_persistence_handles()
    if session_manager is None or config is None:
        return None

    async def _persist(decision: NetworkDecision) -> None:
        await _persist_auto_trusted_host_if_available(
            request,
            runtime,
            decision=decision,
        )

    return _persist


async def _run_with_managed_network_proxy(
    request: SandboxRequest,
    runtime: SandboxRuntime,
) -> SandboxResult:
    managed = await prepare_subprocess_managed_network_proxy(request, runtime=runtime)
    try:
        return await _run_backend_with_platform_network_boundary(managed.request, runtime)
    finally:
        await managed.cleanup()


async def _run_backend_with_platform_network_boundary(
    request: SandboxRequest,
    runtime: SandboxRuntime,
) -> SandboxResult:
    result = await SandboxOperationRuntime(runtime).run(SandboxOperation.process(request))
    if not isinstance(result, SandboxResult):
        raise SandboxBackendError("process sandbox backend returned invalid result")
    return result


def _uses_platform_network_boundary(
    request: SandboxRequest,
    runtime: SandboxRuntime,
) -> bool:
    _ = (request, runtime)
    return False


async def _prepare_platform_network_boundary(
    request: SandboxRequest,
    runtime: SandboxRuntime,
) -> object | None:
    _ = (request, runtime)
    return None


async def _cleanup_platform_network_boundary(context: object | None) -> None:
    if context is None:
        return
    boundary, boundary_context = cast(tuple[Any, Any], context)
    await boundary.cleanup(boundary_context)


async def prepare_subprocess_managed_network_proxy(
    request: SandboxRequest,
    *,
    runtime: SandboxRuntime | None = None,
) -> ManagedNetworkSubprocess:
    """Start the managed proxy needed by a subprocess sandbox request.

    Foreground subprocess tools can await :func:`run_under_backend`; background
    subprocess tools need the proxy to outlive process creation. This helper
    returns a request with ``network_proxy`` populated and an async cleanup
    callback that must run after the subprocess exits or spawn fails.
    """
    if (
        request.policy.network != NetworkMode.PROXY_ALLOWLIST
    ):
        return ManagedNetworkSubprocess(
            request=request,
            cleanup=_noop_managed_network_cleanup,
        )
    rt = runtime or get_runtime()
    backend_name = _backend_name(rt)
    if request.policy.network_proxy is not None:
        return ManagedNetworkSubprocess(
            request=request_with_managed_network_proxy_env(
                request,
                backend_name=backend_name,
            ),
            cleanup=_noop_managed_network_cleanup,
        )

    if rt is None:
        raise SandboxBackendError(
            "Sandbox runtime is not configured; refusing to start managed network proxy"
        )
    context = _current_run_context_for_network_proxy()
    if context is None:
        raise SandboxBackendError(
            "NetworkMode.PROXY_ALLOWLIST requires Run Context grants to start "
            "the managed network proxy"
        )
    grant_workspace = _network_grant_workspace(request, rt)
    context = context_with_temporary_network_grants(
        context,
        fingerprint=action_fingerprint(request),
    )
    context = _context_with_request_package_bundle(context, request)
    service = NetworkApprovalService(
        context=context,
        request=request,
        runtime=rt,
        session_key_override=_resolve_session_id(rt, None),
        workspace_override=grant_workspace,
    )

    on_upstream_opened = _auto_trusted_persistence_callback(
        request,
        rt,
        context=context,
    )
    allowed_ports = _windows_allowed_proxy_ports(rt)
    proxy_port = allowed_ports[0] if allowed_ports else 0
    proxy_lock = _windows_fixed_proxy_port_lock(rt, allowed_ports)
    lock_acquired = False
    if proxy_lock is not None:
        await proxy_lock.acquire()
        lock_acquired = True
    try:
        if on_upstream_opened is None:
            proxy = SandboxProxyServer(service, port=proxy_port)
        else:
            proxy = SandboxProxyServer(
                service,
                port=proxy_port,
                on_upstream_opened=on_upstream_opened,
            )
        await proxy.start()
        if _backend_name(rt).lower().startswith("windows_"):
            if not _windows_proxy_allowlist_enforced(rt, proxy_ports=(proxy.port,)):
                raise SandboxBackendError(
                    "Windows sandbox managed network is unavailable: proxy port is not "
                    "covered by the enforced Windows network boundary."
                )
    except Exception:
        if "proxy" in locals():
            await proxy.stop()
        if lock_acquired and proxy_lock is not None:
            proxy_lock.release()
        raise

    lock_released = False

    async def _cleanup() -> None:
        nonlocal lock_released
        try:
            await proxy.stop()
        finally:
            if lock_acquired and proxy_lock is not None and not lock_released:
                proxy_lock.release()
                lock_released = True

    policy = dataclasses.replace(
        request.policy,
        network_proxy=NetworkProxySpec(host=proxy.host, port=proxy.port),
    )
    return ManagedNetworkSubprocess(
        request=request_with_managed_network_proxy_env(
            request.with_policy(policy),
            backend_name=backend_name,
        ),
        cleanup=_cleanup,
    )


def _windows_fixed_proxy_port_lock(
    runtime: SandboxRuntime | object | None,
    allowed_ports: tuple[int, ...],
) -> asyncio.Lock | None:
    if not _backend_name(runtime).lower().startswith("windows_"):
        return None
    if len(allowed_ports) != 1:
        return None
    global _WINDOWS_FIXED_PROXY_PORT_LOCK
    if _WINDOWS_FIXED_PROXY_PORT_LOCK is None:
        _WINDOWS_FIXED_PROXY_PORT_LOCK = asyncio.Lock()
    return _WINDOWS_FIXED_PROXY_PORT_LOCK


def current_managed_network_proxy_url() -> str | None:
    """Return the context-local managed proxy URL for in-process network tools."""
    return _MANAGED_NETWORK_PROXY_URL.get()


def managed_network_httpx_kwargs() -> dict[str, Any]:
    """Return httpx proxy kwargs for the current managed-network context.

    When a sandboxed in-process network tool is running under
    ``NetworkMode.PROXY_ALLOWLIST``, callers must use an explicit proxy and
    disable ambient env proxy lookup. Outside that context, preserve the
    existing ``opensquilla.env.trust_env()`` behavior.
    """
    proxy_url = current_managed_network_proxy_url()
    if proxy_url:
        return {"proxy": proxy_url, "trust_env": False}
    from opensquilla.env import trust_env

    return {"trust_env": trust_env()}


async def record_success(
    request: SandboxRequest,
    payload: Any,
    *,
    session_id: str | None = None,
    runtime: SandboxRuntime | None = None,
) -> str:
    """Record a successful execution for §8.3 hygiene purposes."""
    rt = runtime or get_runtime()
    cache = rt.cache if rt is not None else get_stale_output_cache()
    sid = _resolve_session_id(rt, session_id) if rt is not None else (session_id or "default")
    return await on_successful_exec(request, payload, session_id=sid, cache=cache)


# ─── Decorator ────────────────────────────────────────────────────────────


HandlerT = Callable[..., Awaitable[Any]]


def sandboxed(
    kind: str,
    *,
    hints: LevelHints | None = None,
    argv_factory: Callable[[dict[str, Any]], tuple[str, ...]] | None = None,
    cwd_factory: Callable[[dict[str, Any]], str | None] | None = None,
    record_payload: bool = True,
) -> Callable[[HandlerT], HandlerT]:
    """Wrap an async tool handler with the sandbox gate.

    Parameters:
        kind: The ``action_kind`` tag (see
            :func:`opensquilla.sandbox.policy.select_level`). Required.
        hints: Optional static :class:`LevelHints`. Tools whose risk profile
            depends on arguments should supply a per-call hints factory by
            using ``@sandboxed`` on a small wrapper instead.
        argv_factory: Custom function to derive the argv-like tuple used for
            fingerprinting. Falls back to a stable serialisation when unset.
        cwd_factory: Custom function to derive the workspace path for the
            call. Falls back to :class:`ToolContext.workspace_dir`.
        record_payload: When ``True`` (the default), record the handler's
            return value in the stale-output cache on success.

    The wrapped handler accepts a hidden keyword argument
    ``_sandbox_followup`` that the agent may set to ``"lower_privilege"``,
    ``"explain"``, or ``"narrower_approval"`` to tag a follow-up after a
    prior denial (see §8.4). The kwarg is consumed before the real handler
    runs so downstream signatures are unaffected.
    """

    def decorator(fn: HandlerT) -> HandlerT:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            followup_raw = kwargs.pop("_sandbox_followup", None)
            followup_tag = _coerce_followup(followup_raw)

            bound_args = _safe_bind(sig, args, kwargs)
            argv = argv_factory(bound_args) if argv_factory else _default_argv(kind, bound_args)
            cwd_raw = cwd_factory(bound_args) if cwd_factory else bound_args.get("workdir")
            cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else None

            from opensquilla.tools.run_mode import full_host_access_active

            if full_host_access_active():
                return await fn(*args, **kwargs)

            decision, policy, request = await gate_action(
                action_kind=kind,
                argv=argv,
                cwd=cwd,
                env=_string_env(bound_args.get("env")),
                followup_tag=followup_tag,
                hints=hints,
            )
            if isinstance(decision, DenialResult):
                return json.dumps(decision.to_dict())

            if policy.network == NetworkMode.NONE and _is_in_process_network_action(kind):
                rt = get_runtime()
                if rt is None:
                    return json.dumps(
                        DenialResult(
                            reason=DenialReason.RUNTIME_UNCONFIGURED,
                            suggested_next_step=SuggestedNextStep.ASK_USER,
                            level=policy.level,
                            action_fingerprint=action_fingerprint(request),
                            message=(
                                "Sandbox runtime is not configured. "
                                "Network-disabled in-process tools refuse to run."
                            ),
                            retryable=False,
                        ).to_dict()
                    )
                prepared = await _prepare_network_none_in_process_action(request, rt)
                if isinstance(prepared, DenialResult):
                    return json.dumps(prepared.to_dict())
                if isinstance(prepared, dict):
                    return json.dumps(prepared)
                return await _run_in_process_with_managed_network(
                    fn,
                    args,
                    kwargs,
                    request=request,
                    runtime=rt,
                    context=prepared,
                )

            if policy.network == NetworkMode.PROXY_ALLOWLIST:
                rt = get_runtime()
                if rt is None:
                    return json.dumps(
                        DenialResult(
                            reason=DenialReason.RUNTIME_UNCONFIGURED,
                            suggested_next_step=SuggestedNextStep.ASK_USER,
                            level=policy.level,
                            action_fingerprint=action_fingerprint(request),
                            message=(
                                "Sandbox runtime is not configured. "
                                "Managed in-process network tools refuse to run."
                            ),
                            retryable=False,
                        ).to_dict()
                    )
                prepared = await _prepare_in_process_managed_network(request, rt)
                if isinstance(prepared, DenialResult):
                    return json.dumps(prepared.to_dict())
                if isinstance(prepared, dict):
                    return json.dumps(prepared)
                result = await _run_in_process_with_managed_network(
                    fn,
                    args,
                    kwargs,
                    request=request,
                    runtime=rt,
                    context=prepared,
                )
            else:
                result = await fn(*args, **kwargs)
            if record_payload:
                try:
                    await record_success(request, result)
                except Exception:  # pragma: no cover - cache failures should never break tools
                    log.exception("sandbox.record_success_failed", extra={"kind": kind})
            return result

        setattr(wrapper, "__sandbox_kind__", kind)
        return wrapper

    return decorator


def _safe_bind(
    sig: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        return dict(kwargs)


def _coerce_followup(raw: Any) -> FollowupTag:
    if raw is None:
        return FollowupTag.NONE
    if isinstance(raw, FollowupTag):
        return raw
    if isinstance(raw, str):
        try:
            return FollowupTag(raw)
        except ValueError:
            return FollowupTag.NONE
    return FollowupTag.NONE


def _string_env(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    return {str(k): str(v) for k, v in value.items()}


async def _prepare_in_process_managed_network(
    request: SandboxRequest,
    runtime: SandboxRuntime,
) -> RunContext | DenialResult | dict[str, object]:
    if not await _platform_proxy_allowlist_ready_or_repaired(runtime):
        return await _managed_in_process_denial(
            request,
            runtime,
            _windows_managed_network_unavailable_message(runtime),
        )

    context = _current_run_context_for_network_proxy()
    if context is None:
        return await _managed_in_process_denial(
            request,
            runtime,
            "NetworkMode.PROXY_ALLOWLIST requires Run Context grants to run "
            "in-process network tools through the managed proxy.",
        )
    fingerprint = action_fingerprint(request)
    context = context_with_temporary_network_grants(
        context,
        fingerprint=fingerprint,
    )
    system_domains = _system_domain_grants_for_request(request)
    effective_context = _context_with_system_domain_grants(context, system_domains)
    artifact_preflight = await _preflight_cached_network_artifact_access(
        request,
        runtime,
        effective_context,
        fingerprint=fingerprint,
    )
    if artifact_preflight is not None:
        return artifact_preflight
    return effective_context


async def _prepare_network_none_in_process_action(
    request: SandboxRequest,
    runtime: SandboxRuntime,
) -> RunContext | DenialResult | dict[str, object]:
    if not await _platform_proxy_allowlist_ready_or_repaired(runtime):
        return await _managed_in_process_denial(
            request,
            runtime,
            _windows_managed_network_unavailable_message(runtime),
        )

    context = _current_run_context_for_network_proxy()
    if context is None:
        return await _managed_in_process_denial(
            request,
            runtime,
            "Network-disabled in-process tools require Run Context grants before "
            "they can request or use network approvals.",
        )
    fingerprint = action_fingerprint(request)
    context = context_with_temporary_network_grants(
        context,
        fingerprint=fingerprint,
    )
    system_domains = _system_domain_grants_for_request(request)
    effective_context = _context_with_system_domain_grants(context, system_domains)
    artifact_preflight = await _preflight_cached_network_artifact_access(
        request,
        runtime,
        effective_context,
        fingerprint=fingerprint,
    )
    if artifact_preflight is not None:
        return artifact_preflight
    return effective_context


async def preflight_subprocess_managed_network(
    request: SandboxRequest,
    runtime: SandboxRuntime,
    *,
    consume_temporary_grants: bool = True,
) -> DenialResult | dict[str, object] | None:
    """Validate subprocess managed-network readiness before proxy execution.

    Real host decisions happen at the proxy boundary through
    :class:`NetworkApprovalService`. This preflight only keeps platform
    fail-closed checks and optional package-bundle approval UX.
    """
    _ = consume_temporary_grants
    if getattr(request.policy, "network", None) != NetworkMode.PROXY_ALLOWLIST:
        return None

    if (
        _backend_name(runtime).lower().startswith("windows_")
        and not _windows_proxy_allowlist_enforced(runtime)
    ):
        return await _managed_in_process_denial(
            request,
            runtime,
            _windows_managed_network_unavailable_message(runtime),
        )

    context = _current_run_context_for_network_proxy()
    if context is None:
        return await _managed_in_process_denial(
            request,
            runtime,
            "NetworkMode.PROXY_ALLOWLIST requires Run Context grants to preflight "
            "subprocess managed-network execution.",
        )

    return await _preflight_request_package_bundle(request, runtime, context)


async def _preflight_cached_network_artifact_access(
    request: SandboxRequest,
    runtime: SandboxRuntime,
    context: RunContext,
    *,
    fingerprint: str,
) -> DenialResult | dict[str, object] | None:
    for host in _cached_network_artifact_hosts(request):
        decision = decide_network_access(host, context)
        if decision.status == "allow":
            continue
        if decision.status == "ask":
            params = build_network_approval_params(
                decision,
                session_key=_resolve_session_id(runtime, None),
                workspace=_network_grant_workspace(request, runtime),
                fingerprint=fingerprint,
                reviewer=_configured_approval_reviewer(runtime),
            )
            if params is not None:
                return request_sandbox_approval(
                    params,
                    message=(
                        "This cached network result is outside the current "
                        "managed-network grants. Resolve this approval and retry."
                    ),
                )
        return await _managed_in_process_denial(
            request,
            runtime,
            (
                "Cached network result denied for "
                f"target {host!r}: {decision.reason}."
            ),
        )
    return None


def _cached_network_artifact_hosts(request: SandboxRequest) -> tuple[str, ...]:
    if request.action_kind != "web.fetch" or len(request.argv) < 2:
        return ()
    raw_url = str(request.argv[1] or "").strip()
    if not raw_url:
        return ()
    try:
        parsed = urlparse(raw_url)
        parsed.port
    except ValueError:
        return ()
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ()
    return (parsed.hostname.lower(),)


async def _preflight_request_package_bundle(
    request: SandboxRequest,
    runtime: SandboxRuntime,
    context: RunContext,
) -> dict[str, object] | DenialResult | None:
    bundle_id = _package_bundle_id_for_request(request)
    if bundle_id is None:
        return None
    if context.run_mode == RunMode.TRUSTED:
        return None

    fingerprint = action_fingerprint(request)
    effective_context = context_with_temporary_network_grants(
        context,
        fingerprint=fingerprint,
    )
    if _context_has_enabled_package_bundle(effective_context, bundle_id):
        return None
    params = build_package_bundle_approval_params(
        bundle_id,
        session_key=_resolve_session_id(runtime, None),
        workspace=_network_grant_workspace(request, runtime),
        fingerprint=fingerprint,
        reviewer=_configured_approval_reviewer(runtime),
    )
    return request_sandbox_approval(
        params,
        message=(
            "This package install needs network access to package registry domains. "
            "Resolve this approval and retry."
        ),
    )


def _context_with_request_package_bundle(
    context: RunContext,
    request: SandboxRequest,
) -> RunContext:
    bundle_id = _package_bundle_id_for_request(request)
    if bundle_id is None or _context_has_enabled_package_bundle(context, bundle_id):
        return context
    if context.run_mode != RunMode.TRUSTED:
        return context
    grant = PackageBundleGrant(bundle_id=bundle_id, scope="chat", source="auto_trusted")
    return dataclasses.replace(context, bundles=context.bundles + (grant,))


def _context_has_enabled_package_bundle(context: RunContext, bundle_id: str) -> bool:
    return any(
        grant.bundle_id == bundle_id and grant.source != "disabled"
        for grant in context.bundles
    )


def _package_bundle_id_for_request(request: SandboxRequest) -> str | None:
    if request.action_kind not in {"shell.exec", "shell.background", "code.exec"}:
        return None
    profile = capability_profile_for_command(request.argv)
    return next(iter(profile.package_bundles), None)


async def _managed_in_process_denial(
    request: SandboxRequest,
    runtime: SandboxRuntime,
    message: str,
) -> DenialResult:
    denial = DenialResult(
        reason=DenialReason.POLICY_DENIED,
        suggested_next_step=SuggestedNextStep.ASK_USER,
        level=request.policy.level,
        action_fingerprint=action_fingerprint(request),
        message=message,
        retryable=False,
    )
    await runtime.ledger.record_denial(
        _resolve_session_id(runtime, None),
        denial.action_fingerprint,
        denial.reason,
        threshold_eligible=False,
    )
    return denial


async def _run_in_process_with_managed_network(
    fn: HandlerT,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    request: SandboxRequest,
    runtime: SandboxRuntime,
    context: RunContext,
) -> Any:
    service = NetworkApprovalService(
        context=context,
        request=request,
        runtime=runtime,
        session_key_override=_resolve_session_id(runtime, None),
        workspace_override=_network_grant_workspace(request, runtime),
    )
    on_upstream_opened = _auto_trusted_persistence_callback(
        request,
        runtime,
        context=context,
    )
    if on_upstream_opened is None:
        proxy = SandboxProxyServer(service)
    else:
        proxy = SandboxProxyServer(
            service,
            on_upstream_opened=on_upstream_opened,
        )
    await proxy.start()
    try:
        proxy_url = f"http://{proxy.host}:{proxy.port}"
        token = _MANAGED_NETWORK_PROXY_URL.set(proxy_url)
        try:
            return await fn(*args, **kwargs)
        finally:
            _MANAGED_NETWORK_PROXY_URL.reset(token)
    finally:
        await proxy.stop()


async def guard_in_process_network_action(
    *,
    action_kind: str,
    argv: tuple[str, ...],
    runtime: SandboxRuntime | None = None,
) -> DenialResult | dict[str, object] | None:
    """Fail-close helper for in-process network paths that bypass decorators.

    Returns a denial only when the resolved sandbox policy requires managed
    networking and the action cannot safely run. A ``None`` result means the
    caller may continue with its existing non-managed behavior.
    """
    decision, policy, request = await gate_action(
        action_kind=action_kind,
        argv=argv,
        runtime=runtime,
    )
    if isinstance(decision, DenialResult):
        return decision
    if policy.network == NetworkMode.NONE and _is_in_process_network_action(action_kind):
        rt = runtime or get_runtime()
        if rt is None:
            return None
        return await _managed_in_process_denial(
            request,
            rt,
            "Sandbox network is disabled for this in-process network tool.",
        )
    if policy.network != NetworkMode.PROXY_ALLOWLIST:
        return None
    rt = runtime or get_runtime()
    if rt is None:
        return None
    prepared = await _prepare_in_process_managed_network(request, rt)
    if isinstance(prepared, (DenialResult, dict)):
        return prepared
    return None


async def run_in_process_network_action(
    *,
    action_kind: str,
    argv: tuple[str, ...],
    callback: Callable[[], Awaitable[Any]],
    runtime: SandboxRuntime | None = None,
) -> Any | DenialResult | dict[str, object]:
    """Run an undecorated in-process network action under sandbox networking.

    Some gateway RPC handlers call provider code directly instead of going
    through a registered tool decorator. This helper gives those paths the
    same fail-closed and managed-proxy behavior as :func:`sandboxed`.
    """
    rt = runtime or get_runtime()
    if rt is None:
        return await callback()
    decision, policy, request = await gate_action(
        action_kind=action_kind,
        argv=argv,
        runtime=rt,
    )
    if isinstance(decision, DenialResult):
        return decision

    if policy.network == NetworkMode.NONE and _is_in_process_network_action(action_kind):
        rt = runtime or get_runtime()
        if rt is None:
            return DenialResult(
                reason=DenialReason.RUNTIME_UNCONFIGURED,
                suggested_next_step=SuggestedNextStep.ASK_USER,
                level=policy.level,
                action_fingerprint=action_fingerprint(request),
                message=(
                    "Sandbox runtime is not configured. "
                    "Network-disabled in-process tools refuse to run."
                ),
                retryable=False,
            )
        prepared = await _prepare_network_none_in_process_action(request, rt)
        if isinstance(prepared, DenialResult):
            return prepared
        if isinstance(prepared, dict):
            return prepared
        return await _run_in_process_with_managed_network(
            callback,
            (),
            {},
            request=request,
            runtime=rt,
            context=prepared,
        )

    if policy.network != NetworkMode.PROXY_ALLOWLIST:
        return await callback()

    rt = runtime or get_runtime()
    if rt is None:
        return DenialResult(
            reason=DenialReason.RUNTIME_UNCONFIGURED,
            suggested_next_step=SuggestedNextStep.ASK_USER,
            level=policy.level,
            action_fingerprint=action_fingerprint(request),
            message=(
                "Sandbox runtime is not configured. "
                "Managed in-process network tools refuse to run."
            ),
            retryable=False,
        )
    prepared = await _prepare_in_process_managed_network(request, rt)
    if isinstance(prepared, DenialResult):
        return prepared
    if isinstance(prepared, dict):
        return prepared
    return await _run_in_process_with_managed_network(
        callback,
        (),
        {},
        request=request,
        runtime=rt,
        context=prepared,
    )


def _is_in_process_network_action(action_kind: str) -> bool:
    return action_kind in _IN_PROCESS_NETWORK_TAGS


def _system_domain_grants_for_request(request: SandboxRequest) -> tuple[str, ...]:
    tool_name = request.argv[0] if request.argv else ""
    if tool_name not in {"web_search", "web_discover"}:
        return ()
    planned_providers = _search_plan_providers_from_argv(request.argv)
    if planned_providers is not None:
        planned_domains: list[str] = []
        for provider in planned_providers:
            for domain in _SEARCH_PROVIDER_SYSTEM_DOMAINS.get(provider, ()):
                if domain not in planned_domains:
                    planned_domains.append(domain)
        return tuple(planned_domains)
    try:
        from opensquilla.tools.builtin.web import (
            get_active_provider,
            get_search_fallback_policy,
        )

        provider = get_active_provider()
        fallback_policy = get_search_fallback_policy()
    except Exception:  # pragma: no cover - defensive against import-time cycles
        return ()

    domains: list[str] = []
    for domain in _SEARCH_PROVIDER_SYSTEM_DOMAINS.get(provider, ()):
        if domain not in domains:
            domains.append(domain)
    if fallback_policy == "network" and provider != "duckduckgo":
        for domain in _SEARCH_PROVIDER_SYSTEM_DOMAINS.get("duckduckgo", ()):
            if domain not in domains:
                domains.append(domain)
    return tuple(domains)


def _search_plan_providers_from_argv(argv: tuple[str, ...]) -> tuple[str, ...] | None:
    for value in argv[1:]:
        if not value.startswith("providers="):
            continue
        providers: list[str] = []
        for provider in value.removeprefix("providers=").split(","):
            normalized = provider.strip().lower()
            if normalized and normalized not in providers:
                providers.append(normalized)
        return tuple(providers)
    return None


def _context_with_system_domain_grants(
    context: RunContext,
    domains: tuple[str, ...],
) -> RunContext:
    if not domains:
        return context
    existing = {grant.domain for grant in context.domains}
    grants = list(context.domains)
    for domain in domains:
        if domain in existing:
            continue
        grants.append(DomainGrant(domain=domain, scope="chat", source="system"))
        existing.add(domain)
    if len(grants) == len(context.domains):
        return context
    return dataclasses.replace(context, domains=tuple(grants))


async def escalate_backend_denial(
    result: SandboxResult,
    request: SandboxRequest,
    policy: SandboxPolicy,
    *,
    runtime: SandboxRuntime | None = None,
    review_action: ElevationAction | None = None,
) -> DenialResult | ElevationGateResult:
    """Suspend one attributable failure for a fresh broader-context review."""
    fp = action_fingerprint(request)
    notes_str = "; ".join(result.backend_notes)
    rt = runtime or get_runtime()
    if rt is None:
        return DenialResult(
            reason=DenialReason.SEATBELT_DENIED,
            suggested_next_step=SuggestedNextStep.ASK_USER,
            level=policy.level,
            action_fingerprint=fp,
            message=f"Sandbox denied the command ({notes_str}); no runtime to escalate.",
            retryable=False,
        )

    if _runtime_is_full_host_access(rt):
        denial = DenialResult(
            reason=DenialReason.SEATBELT_DENIED,
            suggested_next_step=SuggestedNextStep.ASK_USER,
            level=policy.level,
            action_fingerprint=fp,
            message=(
                f"Sandbox denied the command ({notes_str}). Full Host Access is active, "
                "so no sandbox retry was requested."
            ),
            retryable=False,
        )
        return denial

    output = result.stdout
    if result.stderr:
        output = f"{output}{result.stderr}"
    action = review_action or ElevationAction(
        tool_name=request.argv[0] if request.argv else request.action_kind,
        action_kind=request.action_kind,
        argv=request.argv,
        cwd=str(request.cwd),
        sandbox_permissions="require_escalated",
        justification="command failed; retry without sandbox?",
        risk_markers=tuple(result.backend_notes),
    )
    return gate_elevated_action(
        action,
        approval_id=None,
        session_key=_resolve_session_id(rt, None),
        queue=rt.approval_queue,  # type: ignore[arg-type]
        file_system_profile=getattr(policy, "file_system", None),
        metadata={
            "backendRetry": True,
            "sandboxRequestFingerprint": fp,
            "sandboxOriginalOutput": output,
            "sandboxBackend": str(getattr(result, "backend_used", rt.backend.name)),
            "sandboxBackendNotes": list(result.backend_notes),
            "retryReason": action.justification,
        },
    )


async def escalate_unavailable_backend_in_managed_mode(
    error: SandboxBackendError,
    request: SandboxRequest,
    policy: SandboxPolicy,
    *,
    runtime: SandboxRuntime | None = None,
    review_action: ElevationAction | None = None,
) -> DenialResult | ElevationGateResult | None:
    """Turn a missing sandbox backend into one exact Managed host retry."""

    context = current_tool_run_context()
    try:
        mode = (
            context.run_mode
            if context is not None
            else normalize_run_mode(request.run_mode, default=RunMode.STANDARD)
        )
    except ValueError:
        return None
    if mode != RunMode.TRUSTED:
        return None
    rt = runtime or get_runtime()
    if rt is None or not isinstance(rt.backend, UnavailableBackend):
        return None
    reason = str(error) or "sandbox backend unavailable"
    return await escalate_backend_denial(
        SandboxResult(
            returncode=-1,
            stdout="",
            stderr=reason,
            wall_time_s=0.0,
            timed_out=False,
            backend_used=rt.backend.name,
            backend_notes=("backend_unavailable", reason),
        ),
        request,
        policy,
        runtime=rt,
        review_action=review_action,
    )


def consume_backend_denial_retry(
    approval_id: str | None,
    request: SandboxRequest,
    policy: SandboxPolicy,
    *,
    runtime: SandboxRuntime | None = None,
) -> ElevationGateResult | None:
    """Consume an approved retry only when it still names the same request."""

    if not approval_id:
        return None
    rt = runtime or get_runtime()
    if rt is None:
        return None
    try:
        entry = rt.approval_queue.get(approval_id)
    except (AttributeError, KeyError):
        return None
    if not bool(entry.params.get("backendRetry")):
        return None
    if str(entry.params.get("sandboxRequestFingerprint") or "") != action_fingerprint(request):
        return ElevationGateResult(
            requested=True,
            allowed=False,
            status="approval_action_mismatch",
            approval_id=approval_id,
            reason="approval_action_mismatch",
        )
    raw_action = entry.params.get("action")
    if not isinstance(raw_action, dict):
        return ElevationGateResult(
            requested=True,
            allowed=False,
            status="approval_action_mismatch",
            approval_id=approval_id,
            reason="approval_action_mismatch",
        )
    try:
        action = ElevationAction.from_canonical_payload(raw_action)
    except ValueError:
        return ElevationGateResult(
            requested=True,
            allowed=False,
            status="approval_action_mismatch",
            approval_id=approval_id,
            reason="approval_action_mismatch",
        )
    profile = getattr(policy, "file_system", None)
    if profile is not None and not profile.unsandboxed_execution_allowed:
        return ElevationGateResult(
            requested=True,
            allowed=False,
            status="elevation_forbidden_denied_reads",
            approval_id=approval_id,
            reason=(
                "Unsandboxed retry cannot be granted while the active filesystem "
                "profile contains denied reads."
            ),
        )
    return consume_approved_elevation(
        rt.approval_queue,  # type: ignore[arg-type]
        approval_id,
        action,
        expected_session_key=_resolve_session_id(rt, None),
    )


def _runtime_is_full_host_access(runtime: SandboxRuntime) -> bool:
    try:
        from opensquilla.tools.run_mode import current_run_mode, full_host_access_active

        if current_run_mode() is not None:
            return full_host_access_active()
    except Exception:  # pragma: no cover - defensive against tool-context imports
        pass

    context = current_tool_run_context()
    if context is not None:
        return context.run_mode == RunMode.FULL
    if runtime.settings.run_mode is not None:
        return normalize_run_mode(runtime.settings.run_mode) == RunMode.FULL
    return False


__all__ = [
    "SandboxRuntime",
    "active_file_system_profile",
    "action_fingerprint",
    "build_request",
    "configure_runtime",
    "consume_backend_denial_retry",
    "current_managed_network_proxy_url",
    "escalate_backend_denial",
    "escalate_unavailable_backend_in_managed_mode",
    "gate_action",
    "get_runtime",
    "guard_in_process_network_action",
    "managed_network_httpx_kwargs",
    "ManagedNetworkSubprocess",
    "preflight_subprocess_managed_network",
    "prepare_subprocess_managed_network_proxy",
    "request_with_managed_network_proxy_env",
    "record_success",
    "refresh_runtime_backend_after_setup",
    "reset_runtime",
    "run_in_process_network_action",
    "run_under_backend",
    "sandboxed",
]
