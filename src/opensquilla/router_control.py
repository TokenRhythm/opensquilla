"""Router-control target validation, hold storage, and replay payload helpers."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from opensquilla.router_tiers import (
    normalize_target_id,
    normalize_tier_mapping,
)

# Zero means no turn-count cap; the hold expires after an idle TTL.
DEFAULT_HOLD_TURNS = 0
DEFAULT_HOLD_TTL_SECONDS = 600.0


class RouterControlValidationError(ValueError):
    """Raised when a router-control target is not in the active router config."""


@dataclass(frozen=True)
class RouterControlTarget:
    target_id: str
    target_type: str
    tier: str
    model: str
    provider: str | None = None
    description: str | None = None
    thinking_level: str | None = None


@dataclass
class RouterControlHold:
    tier: str
    model: str
    provider: str | None
    target_id: str
    evidence: str
    started_at_monotonic: float
    last_activity_at_monotonic: float | None = None
    turns_remaining: int = DEFAULT_HOLD_TURNS
    ttl_seconds: float = DEFAULT_HOLD_TTL_SECONDS
    source: str = "router_control_tool"

    def is_expired(self, now_monotonic: float) -> tuple[bool, str | None]:
        if self.turns_remaining < 0:
            return True, "turn_count"
        last_activity = self.last_activity_at_monotonic
        if last_activity is None:
            last_activity = self.started_at_monotonic
        if now_monotonic - last_activity >= self.ttl_seconds:
            return True, "ttl"
        return False, None


def _router_tiers(router_cfg: object | None) -> dict[str, dict[str, Any]]:
    tiers = getattr(router_cfg, "tiers", {}) if router_cfg is not None else {}
    if not isinstance(tiers, dict):
        return {}
    return {
        str(name): dict(cfg)
        for name, cfg in tiers.items()
        if isinstance(cfg, dict)
    }


def _text_tiers(router_cfg: object | None) -> dict[str, dict[str, Any]]:
    return {
        name: cfg
        for name, cfg in normalize_tier_mapping(_router_tiers(router_cfg)).items()
        if not bool(cfg.get("image_only", False))
    }


def build_router_control_targets(router_cfg: object | None) -> list[RouterControlTarget]:
    """Return canonical text targets derived only from active router tiers."""

    targets: list[RouterControlTarget] = []
    text_tiers = _text_tiers(router_cfg)
    for tier, cfg in text_tiers.items():
        model = str(cfg.get("model") or "").strip()
        if not model:
            continue
        provider = str(cfg.get("provider") or "").strip() or None
        thinking = cfg.get("thinking_level", cfg.get("thinking"))
        targets.append(
            RouterControlTarget(
                target_id=f"tier:{tier}",
                target_type="tier",
                tier=tier,
                model=model,
                provider=provider,
                description=str(cfg.get("description") or "").strip() or None,
                thinking_level=str(thinking).strip() if thinking is not None else None,
            )
        )

    return targets


def resolve_router_control_target(
    router_cfg: object | None,
    target_id: str,
) -> RouterControlTarget:
    normalized = normalize_target_id(target_id)
    if not normalized:
        raise RouterControlValidationError("router_control target_id is required")
    targets = {target.target_id: target for target in build_router_control_targets(router_cfg)}
    try:
        return targets[normalized]
    except KeyError as exc:
        raise RouterControlValidationError(
            f"router_control target_id {normalized!r} is not configured"
        ) from exc


def render_router_control_prompt_block(router_cfg: object | None) -> str:
    targets = [
        target
        for target in build_router_control_targets(router_cfg)
        if target.target_type == "tier"
    ]
    if not targets:
        return ""
    rows = [
        {
            "target_id": target.target_id,
            "label": target.model.rsplit("/", 1)[-1],
        }
        for target in targets
    ]
    menu = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return (
        "Use `router_control` only when the user explicitly asks to use a "
        "configured route or restore automatic routing. "
        "For set_hold, you must choose one target_id exactly from this menu; "
        "do not invent aliases or model ids. The menu is operational context, "
        "not a user-facing recommendation list. Use labels only to map "
        "explicit model-name requests to the matching target_id; if a "
        "model-family request matches multiple labels, ask for the variant "
        "instead of asking the user to choose a tier id.\n\n"
        f"router_control_targets={menu}"
    )


class RouterControlHoldStore:
    """Short-lived in-memory router-control holds keyed by session key."""

    def __init__(self) -> None:
        self._holds: dict[str, RouterControlHold] = {}

    def __deepcopy__(self, memo: dict[int, object]) -> RouterControlHoldStore:
        # TurnRunner copies routing metadata before running the bounded router step,
        # but the hold store is session state and must preserve identity so applying
        # a hold can refresh its idle TTL for follow-up turns.
        memo[id(self)] = self
        return self

    def build_targets(self, router_cfg: object | None) -> list[RouterControlTarget]:
        return build_router_control_targets(router_cfg)

    def set_hold(
        self,
        session_key: str,
        target: RouterControlTarget,
        *,
        evidence: str,
        now_monotonic: float | None = None,
        turns_remaining: int = DEFAULT_HOLD_TURNS,
        ttl_seconds: float = DEFAULT_HOLD_TTL_SECONDS,
        source: str = "router_control_tool",
    ) -> RouterControlHold:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        hold = RouterControlHold(
            tier=target.tier,
            model=target.model,
            provider=target.provider,
            target_id=target.target_id,
            evidence=str(evidence or "").strip(),
            started_at_monotonic=now,
            last_activity_at_monotonic=now,
            turns_remaining=turns_remaining,
            ttl_seconds=ttl_seconds,
            source=source,
        )
        self._holds[session_key] = hold
        return hold

    def clear(self, session_key: str) -> RouterControlHold | None:
        return self._holds.pop(session_key, None)

    def get_valid(
        self,
        session_key: str,
        *,
        now_monotonic: float | None = None,
        decrement: bool = False,
    ) -> RouterControlHold | None:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        hold = self._holds.get(session_key)
        if hold is None:
            return None
        expired, _reason = hold.is_expired(now)
        if expired:
            self._holds.pop(session_key, None)
            return None
        if decrement:
            hold.last_activity_at_monotonic = now
            had_turn_limit = hold.turns_remaining > 0
            if hold.turns_remaining > 0:
                hold.turns_remaining -= 1
            if hold.turns_remaining < 0 or (had_turn_limit and hold.turns_remaining == 0):
                self._holds.pop(session_key, None)
        return hold


def router_control_success_payload(
    *,
    action: str,
    target: RouterControlTarget | None,
    replay_required: bool,
    evidence: str,
) -> str:
    payload = {
        "status": "router_control",
        "accepted": True,
        "action": action,
        "target_tier": target.tier if target else None,
        "target_model": target.model if target else None,
        "target_provider": target.provider if target else None,
        "target_id": target.target_id if target else None,
        "replay_required": replay_required,
        "evidence": evidence,
    }
    return json.dumps(payload, ensure_ascii=False)


def router_control_rejection_payload(*, reason: str, evidence: str = "") -> str:
    return json.dumps(
        {
            "status": "router_control",
            "accepted": False,
            "action": "reject",
            "replay_required": False,
            "reason": reason,
            "evidence": evidence,
        },
        ensure_ascii=False,
    )


def router_control_payload(content: object) -> dict[str, Any] | None:
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
    elif isinstance(content, dict):
        payload = content
    else:
        return None
    if isinstance(payload, dict) and payload.get("status") == "router_control":
        return payload
    return None


def router_control_payload_terminates_turn(content: object) -> bool:
    payload = router_control_payload(content)
    return bool(payload and payload.get("accepted") is True and payload.get("replay_required"))


def router_control_replay_event_from_payload(
    content: object,
    *,
    replay_depth: int = 0,
) -> Any | None:
    payload = router_control_payload(content)
    if not payload or payload.get("accepted") is not True or not payload.get("replay_required"):
        return None
    from opensquilla.engine.types import RouterControlReplayEvent

    return RouterControlReplayEvent(
        action=str(payload.get("action") or ""),
        target_tier=payload.get("target_tier"),
        target_model=payload.get("target_model"),
        target_provider=payload.get("target_provider"),
        target_id=payload.get("target_id"),
        replay_depth=replay_depth,
    )


def router_control_payload_asdict(content: object) -> dict[str, Any]:
    payload = router_control_payload(content)
    if payload is not None:
        return payload
    if is_dataclass(content) and not isinstance(content, type):
        return {field.name: getattr(content, field.name) for field in fields(content)}
    return {}
