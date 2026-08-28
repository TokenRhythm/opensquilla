"""Proposals domain RPC handlers backed by opensquilla.skills.proposals_lib.

Five JSON-RPC methods drive the WebUI proposals panel:

* ``exec.proposals.pending_count`` — cheap badge count
* ``exec.proposals.list``         — table of pending proposals
* ``exec.proposals.show``         — full SKILL.md + gates payload
* ``exec.proposals.accept``       — promote to MANAGED layer
* ``exec.proposals.reject``       — delete the proposal directory

All five run in-process by calling ``proposals_lib`` directly (no
subprocess fork per click). All five validate ``proposal_id`` with
the 8-hex regex BEFORE touching the filesystem — accept/reject are
irreversible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.paths import default_opensquilla_home
from opensquilla.skills import proposals_lib

_d = get_dispatcher()


def _home() -> Path:
    from opensquilla.gateway.auto_propose_bridge import get_runtime

    rt = get_runtime()
    if rt is not None:
        return rt.home
    return default_opensquilla_home()


def _invalidate_loader(ctx: RpcContext) -> None:
    loader = getattr(ctx, "skill_loader", None)
    if loader is not None:
        invalidate = getattr(loader, "invalidate_cache", None)
        if invalidate is not None:
            invalidate()


def _require_proposal_id(params: dict | None) -> str:
    if not isinstance(params, dict):
        raise ValueError("params object required")
    pid = params.get("proposal_id") or params.get("proposalId")
    if not isinstance(pid, str) or not proposals_lib.is_valid_proposal_id(pid):
        raise ValueError(
            "proposal_id must be 8 lowercase hex chars",
        )
    return pid


def _require_skill_name(params: dict | None) -> str:
    if not isinstance(params, dict):
        raise ValueError("params object required")
    name = params.get("name")
    if not isinstance(name, str) or not proposals_lib.SKILL_NAME_PATTERN.fullmatch(name):
        raise ValueError("name must be a valid skill name")
    return name


@_d.method("exec.proposals.pending_count", scope="operator.proposals")
async def _handle_pending_count(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return proposals_lib.pending_count(_home())


@_d.method("exec.proposals.list", scope="operator.proposals")
async def _handle_list(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return proposals_lib.list_proposals(_home())


@_d.method("exec.proposals.show", scope="operator.proposals")
async def _handle_show(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    pid = _require_proposal_id(params)
    return proposals_lib.show_proposal(_home(), pid)


@_d.method("exec.proposals.accept", scope="operator.admin")
async def _handle_accept(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    pid = _require_proposal_id(params)
    force = bool((params or {}).get("force", False))
    result = proposals_lib.accept_proposal(_home(), pid, force=force)
    if result.get("status") == "ok":
        _invalidate_loader(ctx)
    return result


@_d.method("exec.proposals.reject", scope="operator.admin")
async def _handle_reject(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    pid = _require_proposal_id(params)
    return proposals_lib.reject_proposal(_home(), pid)


@_d.method("exec.proposals.auto_enabled.list", scope="operator.proposals")
async def _handle_auto_enabled_list(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return proposals_lib.list_auto_enabled_skills(_home())


@_d.method("exec.proposals.auto_enabled.disable", scope="operator.admin")
async def _handle_auto_enabled_disable(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    name = _require_skill_name(params)
    result = proposals_lib.disable_auto_enabled_skill(_home(), name)
    if result.get("status") == "ok":
        _invalidate_loader(ctx)
    return result


# ─── Settings: WebUI toggle for the auto-propose feature ──────────────


def _settings_payload(cfg: Any, available: bool) -> dict[str, Any]:
    return {
        "available": available,
        "enabled": bool(getattr(cfg, "enabled", False)) if cfg is not None else False,
        "on_dream_complete": (
            bool(getattr(cfg, "on_dream_complete", False))
            if cfg is not None
            else False
        ),
        "auto_enable": (
            bool(getattr(cfg, "auto_enable", False))
            if cfg is not None
            else False
        ),
        "auto_enable_max_risk": (
            str(getattr(cfg, "auto_enable_max_risk", "low"))
            if cfg is not None
            else "low"
        ),
        "cron": getattr(cfg, "cron", "0 5 * * *") if cfg is not None else "0 5 * * *",
        "window_days": (
            int(getattr(cfg, "window_days", 30)) if cfg is not None else 30
        ),
        "min_freq": int(getattr(cfg, "min_freq", 3)) if cfg is not None else 3,
        "top_k": int(getattr(cfg, "top_k", 5)) if cfg is not None else 5,
    }


@_d.method("exec.proposals.settings.get", scope="operator.proposals")
async def _handle_settings_get(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    """Return the live auto-propose runtime settings.

    When the runtime isn't registered (provider not configured, or the
    feature surface failed to wire at boot), ``available`` is ``False``
    and the UI shows a "feature unavailable" hint instead of toggles.
    """
    from opensquilla.gateway.auto_propose_bridge import get_runtime

    rt = get_runtime()
    if rt is None:
        return _settings_payload(cfg=None, available=False)
    return _settings_payload(cfg=rt.config, available=True)


@_d.method("exec.proposals.settings.set", scope="operator.admin")
async def _handle_settings_set(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    """Mutate the live runtime config + persist to JSON state file.

    Side effect: when ``enabled`` transitions ``False → True`` the
    per-agent cron jobs are added; the reverse transition pauses them
    (idempotent re-register-or-pause). Dream-hook is purely
    predicate-gated so its toggle has no scheduler side effect.

    Accepts partial updates — clients may pass only the keys they want
    to change.
    """
    from opensquilla.gateway.auto_propose_bridge import get_runtime
    from opensquilla.skills.proposals_lib import write_auto_propose_settings

    rt = get_runtime()
    if rt is None:
        return {"status": "error", "reason": "auto_propose runtime not available"}
    if not isinstance(params, dict):
        raise ValueError("params object required")

    cfg = rt.config
    was_enabled = bool(getattr(cfg, "enabled", False))
    old_values = {
        "enabled": was_enabled,
        "on_dream_complete": bool(getattr(cfg, "on_dream_complete", False)),
        "auto_enable": bool(getattr(cfg, "auto_enable", False)),
        "auto_enable_max_risk": str(getattr(cfg, "auto_enable_max_risk", "low")),
    }
    requested: dict[str, Any] = {}
    for key in ("enabled", "on_dream_complete", "auto_enable"):
        if key in params:
            v = params[key]
            if not isinstance(v, bool):
                raise ValueError(f"{key} must be a boolean")
            requested[key] = v
    if "auto_enable_max_risk" in params:
        risk = params["auto_enable_max_risk"]
        if risk not in proposals_lib.RISK_LEVELS:
            raise ValueError("auto_enable_max_risk must be one of low, medium, high")
        requested["auto_enable_max_risk"] = risk

    new_values = dict(old_values)
    new_values.update(requested)
    now_enabled = bool(new_values["enabled"])

    # Apply scheduler side effects before mutating/persisting state. If the
    # scheduler update fails, the live config and JSON state remain untouched.
    try:
        if now_enabled and not was_enabled:
            await rt.register_crons()
        elif was_enabled and not now_enabled:
            await rt.pause_crons()
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "reason": f"failed to update scheduler: {exc}",
            "settings": _settings_payload(cfg, available=True),
        }

    # Apply to the live object the predicate reads after scheduler success.
    for key, value in new_values.items():
        setattr(cfg, key, value)

    # Persist so the toggle survives restart
    persisted = {
        "enabled": bool(getattr(cfg, "enabled", False)),
        "on_dream_complete": bool(getattr(cfg, "on_dream_complete", False)),
        "auto_enable": bool(getattr(cfg, "auto_enable", False)),
        "auto_enable_max_risk": str(getattr(cfg, "auto_enable_max_risk", "low")),
    }
    try:
        write_auto_propose_settings(rt.home, persisted)
    except OSError as exc:
        for key, value in old_values.items():
            setattr(cfg, key, value)
        try:
            if now_enabled and not was_enabled:
                await rt.pause_crons()
            elif was_enabled and not now_enabled:
                await rt.register_crons()
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": "error",
            "reason": f"failed to persist settings: {exc}",
            "settings": _settings_payload(cfg, available=True),
        }

    return {"status": "ok", "settings": _settings_payload(cfg, available=True)}
