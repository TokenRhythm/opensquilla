"""Execution: carry out an :class:`UninstallPlan` behind safety guards.

The planner already decided *what*; this module does it, re-checking every
deletion against :mod:`~opensquilla.uninstall.safety` (defense in depth — a plan
bug must still not delete outside a resolved, non-protected root). Order matters:
the gateway is quiesced first, and if a live gateway cannot be stopped, execution
**aborts before any file deletion** so files are never removed out from under a
running process.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opensquilla.uninstall import safety
from opensquilla.uninstall.inventory import Inventory
from opensquilla.uninstall.plan import Action, UninstallPlan

# Injected by the CLI layer so the uninstall core never imports `cli` (avoids a
# cli<->uninstall import cycle). Given (host, port, config_path, shutdown_timeout),
# stop the lifecycle-managed gateway and return (state, exit_code, message).
LifecycleStop = Callable[[str, int, "str | None", float], "tuple[str, int, str]"]


@dataclass
class ActionResult:
    kind: str
    summary: str
    ok: bool
    detail: str = ""
    paths: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "summary": self.summary,
            "ok": self.ok,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.paths:
            payload["paths"] = self.paths
        return payload


@dataclass
class ExecutionResult:
    results: list[ActionResult] = field(default_factory=list)
    ok: bool = True
    aborted: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "aborted": self.aborted,
            "results": [r.to_payload() for r in self.results],
        }


def execute(
    plan: UninstallPlan,
    inventory: Inventory,
    *,
    lifecycle_stop: LifecycleStop | None = None,
) -> ExecutionResult:
    """Execute ``plan``. Returns an :class:`ExecutionResult` (never raises for
    expected failures; collects per-action outcomes).

    ``lifecycle_stop`` is supplied by the CLI to stop the lifecycle-managed
    gateway (kept out of this package to avoid a cli<->uninstall import cycle).
    When omitted, only the port-independent ``gateway.pid`` liveness backstop
    runs — still fail-closed, but it cannot actively stop a managed process.
    """
    result = ExecutionResult()
    home = inventory.home
    # Explicit allowlist of roots a `remove-tree` may target: the home itself and
    # each portable program tree. A tree-root removal must resolve to exactly one
    # of these — never to an arbitrary path.parent — so a plan/receipt bug can't
    # widen the blast radius (the protected-root check is then a second gate).
    trusted_roots = {safety.resolve_real(home)}
    for program in inventory.program_paths:
        trusted_roots.add(safety.resolve_real(program))

    for action in plan.actions:
        if action.kind == "stop-gateway":
            r = _quiesce_gateway(inventory, lifecycle_stop)
            result.results.append(r)
            if not r.ok:
                # A live gateway we could not stop — do NOT delete files under a
                # running process. Abort before anything destructive.
                result.ok = False
                result.aborted = True
                return result
            continue

        if action.kind == "unregister-service":
            result.results.append(_unregister_service(action))
            continue

        if action.kind == "run-package-uninstall":
            r = _run_commands(action)
            result.results.append(r)
            result.ok = result.ok and r.ok
            continue

        if action.kind in ("remove-path", "remove-tree"):
            is_root = action.kind == "remove-tree"
            r = _remove_paths(
                action, home=home, trusted_roots=trusted_roots, is_root_removal=is_root
            )
            result.results.append(r)
            result.ok = result.ok and r.ok
            continue

        # Unknown action kinds are recorded but not acted on.
        result.results.append(ActionResult(action.kind, action.summary, ok=True, detail="no-op"))

    return result


def _quiesce_gateway(
    inventory: Inventory, lifecycle_stop: LifecycleStop | None = None
) -> ActionResult:
    """Stop the gateway before any deletion. Fail CLOSED: when liveness cannot be
    determined, refuse (ok=False) so files are never deleted under a live process.
    """
    try:
        host, port, state_dirs = _gateway_quiesce_targets(inventory)

        # 1. Stop the lifecycle-managed gateway via the CLI-provided callable.
        if lifecycle_stop is not None:
            from opensquilla.gateway.boot import gateway_shutdown_deadline

            config_path = str(inventory.config_path) if inventory.config_path else None
            state, exit_code, message = lifecycle_stop(
                host, port, config_path, gateway_shutdown_deadline()
            )
            if state in ("unmanaged", "target_mismatch"):
                return ActionResult(
                    "stop-gateway",
                    "A gateway is running that this command cannot stop",
                    ok=False,
                    detail=f"state={state}; stop it (opensquilla gateway stop) and re-run.",
                )
            if state not in ("not_started", "stale") and exit_code != 0:
                return ActionResult(
                    "stop-gateway",
                    "Could not stop the running gateway",
                    ok=False,
                    detail=message or state,
                )

        # 2. Port-independent backstop: a live gateway.pid (written by EVERY gateway
        # run — foreground / desktop / unmanaged — unlike the lifecycle gateway.json
        # that only `gateway start` writes) means a gateway still holds this
        # profile, so refuse to delete if any live pid remains.
        live = _live_gateway_pid(state_dirs)
        if live is not None:
            return ActionResult(
                "stop-gateway",
                "A gateway is still running on this profile",
                ok=False,
                detail=f"pid {live} is alive; stop it (opensquilla gateway stop) and re-run.",
            )

        return ActionResult("stop-gateway", "Gateway quiesced", ok=True, detail="ok")
    except Exception as exc:  # noqa: BLE001 — destructive op: unknown state must block
        return ActionResult(
            "stop-gateway",
            "Could not determine gateway state; refusing to delete",
            ok=False,
            detail=f"{exc}; stop the gateway manually (opensquilla gateway stop) and re-run.",
        )


def _gateway_quiesce_targets(inventory: Inventory) -> tuple[str, int, set[Path]]:
    """Resolve (host, port, state-dir candidates) for the quiesce probe.

    Host/port come from the gateway config so the lifecycle probe targets the real
    endpoint. State-dir candidates are where a ``gateway.pid`` could live (the
    home's ``state/`` and any relocated ``config.state_dir``).
    """
    host = "127.0.0.1"
    port = 18791
    state_dirs: set[Path] = {inventory.state_root}
    try:
        from opensquilla.gateway.config import GatewayConfig

        cfg = GatewayConfig.load(str(inventory.config_path) if inventory.config_path else None)
        host = cfg.host or "127.0.0.1"
        port = cfg.port
        relocated = getattr(cfg, "state_dir", None)
        if isinstance(relocated, str) and relocated.strip():
            state_dirs.add(Path(relocated).expanduser())
    except Exception:  # noqa: BLE001 — config is advisory here; pid backstop is the guard
        pass
    return host, port, state_dirs


def _live_gateway_pid(state_dirs: set[Path]) -> int | None:
    """Return a live PID from any ``<state_dir>/gateway.pid``, else None."""
    try:
        from opensquilla.gateway.pidlock import _is_alive, _read_pid_from_path
    except ImportError:
        return None
    for state_dir in state_dirs:
        pid = _read_pid_from_path(Path(state_dir) / "gateway.pid")
        if pid is not None and _is_alive(pid):
            return pid
    return None


def _unregister_service(action: Action) -> ActionResult:
    """Run a service's unregister commands, then remove its unit file (best-effort)."""
    detail_parts: list[str] = []
    for command in action.commands:
        code = _run_one(command)
        detail_parts.append(f"{command[0]}={code}")
    removed: list[str] = []
    for raw in action.paths:
        path = Path(raw)
        if path.exists() and safety.is_within(path, safety.home_dir()):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError as exc:
                detail_parts.append(f"rm-failed:{exc}")
    # Service teardown is best-effort: a missing/already-disabled unit is fine.
    return ActionResult(
        "unregister-service",
        action.summary,
        ok=True,
        detail="; ".join(detail_parts),
        paths=removed,
    )


def _run_commands(action: Action) -> ActionResult:
    ok = True
    details: list[str] = []
    for command in action.commands:
        code = _run_one(command)
        details.append(f"{' '.join(command)} -> exit {code}")
        ok = ok and code == 0
    return ActionResult(action.kind, action.summary, ok=ok, detail="; ".join(details))


def _run_one(command: list[str]) -> int:
    try:
        completed = subprocess.run(  # noqa: S603 - argv built internally, shell=False
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return completed.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        return _COMMAND_ERROR if isinstance(exc, OSError) else _COMMAND_TIMEOUT


_COMMAND_ERROR = 127
_COMMAND_TIMEOUT = 124


def _remove_paths(
    action: Action, *, home: Path, trusted_roots: set[Path], is_root_removal: bool
) -> ActionResult:
    removed: list[str] = []
    failures: list[str] = []
    for raw in action.paths:
        path = Path(raw)
        ok, detail = _safe_remove(
            path, home=home, trusted_roots=trusted_roots, is_root_removal=is_root_removal
        )
        if ok:
            if detail != "absent":
                removed.append(str(path))
        else:
            failures.append(f"{path}: {detail}")
    return ActionResult(
        action.kind,
        action.summary,
        ok=not failures,
        detail="; ".join(failures),
        paths=removed,
    )


def _safe_remove(
    path: Path, *, home: Path, trusted_roots: set[Path], is_root_removal: bool
) -> tuple[bool, str]:
    """Delete ``path`` only if it passes containment + protected-root checks."""
    if is_root_removal:
        # A whole-tree removal must (1) not be a protected/dangerous root and
        # (2) resolve to an explicitly trusted root (the home or a known program
        # tree) — not merely live under its own parent.
        if safety.is_protected_root(path):
            return False, f"refused: protected root ({safety.protected_root_reason(path)})"
        if safety.resolve_real(path) not in trusted_roots:
            return False, "refused: not a trusted removal root"
        is_symlink = path.is_symlink()
    else:
        # A bucket/file removal must live within the OpenSquilla home. For a
        # symlink, validate where the link *lives* (its parent), not where it
        # points — we delete the link itself, never follow it.
        is_symlink = path.is_symlink()
        containment_target = path.parent if is_symlink else path
        if not safety.is_within(containment_target, home):
            return False, "refused: outside the OpenSquilla home"
    try:
        if is_symlink:
            # Remove the link itself, never follow it into its target tree.
            path.unlink()
            return True, "removed-symlink"
        if path.is_dir():
            shutil.rmtree(path)
            return True, "removed-tree"
        if path.exists():
            path.unlink()
            return True, "removed-file"
        return True, "absent"
    except OSError as exc:
        return False, f"error: {exc}"
