"""Transport-neutral Skill proposal review and auto-proposal settings use cases."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

_PROPOSAL_ID = re.compile(r"[0-9a-f]{8}")
_SKILL_NAME = re.compile(r"[\w\-]+")
_RISK_LEVELS = frozenset({"low", "medium", "high"})


@dataclass(frozen=True, slots=True)
class ProposalSettings:
    enabled: bool = False
    on_dream_complete: bool = False
    auto_enable: bool = False
    auto_enable_max_risk: str = "low"
    cron: str = "0 5 * * *"
    window_days: int = 30
    min_freq: int = 3
    top_k: int = 5

    def __post_init__(self) -> None:
        if self.auto_enable_max_risk not in _RISK_LEVELS:
            raise ValueError("auto_enable_max_risk must be one of low, medium, high")

    def to_public_dict(self, *, available: bool) -> dict[str, Any]:
        return {
            "available": available,
            "enabled": self.enabled,
            "on_dream_complete": self.on_dream_complete,
            "auto_enable": self.auto_enable,
            "auto_enable_max_risk": self.auto_enable_max_risk,
            "cron": self.cron,
            "window_days": self.window_days,
            "min_freq": self.min_freq,
            "top_k": self.top_k,
        }


@dataclass(frozen=True, slots=True)
class ProposalSettingsPatch:
    enabled: bool | None = None
    on_dream_complete: bool | None = None
    auto_enable: bool | None = None
    auto_enable_max_risk: str | None = None

    def __post_init__(self) -> None:
        if (
            self.auto_enable_max_risk is not None
            and self.auto_enable_max_risk not in _RISK_LEVELS
        ):
            raise ValueError("auto_enable_max_risk must be one of low, medium, high")

    def apply(self, current: ProposalSettings) -> ProposalSettings:
        return ProposalSettings(
            enabled=current.enabled if self.enabled is None else self.enabled,
            on_dream_complete=(
                current.on_dream_complete
                if self.on_dream_complete is None
                else self.on_dream_complete
            ),
            auto_enable=(
                current.auto_enable if self.auto_enable is None else self.auto_enable
            ),
            auto_enable_max_risk=(
                current.auto_enable_max_risk
                if self.auto_enable_max_risk is None
                else self.auto_enable_max_risk
            ),
            cron=current.cron,
            window_days=current.window_days,
            min_freq=current.min_freq,
            top_k=current.top_k,
        )


class SkillProposalStorePort(Protocol):
    async def list_proposals(self) -> Mapping[str, Any]: ...

    async def show_proposal(self, proposal_id: str) -> Mapping[str, Any]: ...

    async def accept_proposal(
        self, proposal_id: str, *, force: bool
    ) -> Mapping[str, Any]: ...

    async def reject_proposal(self, proposal_id: str) -> Mapping[str, Any]: ...

    async def list_auto_enabled(self) -> Mapping[str, Any]: ...

    async def disable_auto_enabled(self, name: str) -> Mapping[str, Any]: ...

    def invalidate_catalog(self) -> None: ...


class AutoProposalRuntimePort(Protocol):
    def snapshot(self) -> ProposalSettings | None: ...

    async def transition_schedule(self, *, was_enabled: bool, now_enabled: bool) -> None: ...

    def apply_live(self, settings: ProposalSettings) -> None: ...

    def persist(self, settings: ProposalSettings) -> None: ...


class SkillProposalReview:
    """Own proposal identity checks and failure-safe settings orchestration."""

    def __init__(
        self,
        proposals: SkillProposalStorePort,
        runtime: AutoProposalRuntimePort,
    ) -> None:
        self._proposals = proposals
        self._runtime = runtime

    async def list_proposals(self) -> Mapping[str, Any]:
        return await self._proposals.list_proposals()

    async def show_proposal(self, proposal_id: str) -> Mapping[str, Any]:
        return await self._proposals.show_proposal(self._proposal_id(proposal_id))

    async def accept_proposal(
        self, proposal_id: str, *, force: bool = False
    ) -> Mapping[str, Any]:
        result = await self._proposals.accept_proposal(
            self._proposal_id(proposal_id), force=force
        )
        if result.get("status") == "ok":
            self._proposals.invalidate_catalog()
        return result

    async def reject_proposal(self, proposal_id: str) -> Mapping[str, Any]:
        return await self._proposals.reject_proposal(self._proposal_id(proposal_id))

    async def list_auto_enabled(self) -> Mapping[str, Any]:
        return await self._proposals.list_auto_enabled()

    async def disable_auto_enabled(self, name: str) -> Mapping[str, Any]:
        result = await self._proposals.disable_auto_enabled(self._skill_name(name))
        if result.get("status") == "ok":
            self._proposals.invalidate_catalog()
        return result

    def settings(self) -> Mapping[str, Any]:
        current = self._runtime.snapshot()
        if current is None:
            return ProposalSettings().to_public_dict(available=False)
        return current.to_public_dict(available=True)

    async def update_settings(self, patch: ProposalSettingsPatch) -> Mapping[str, Any]:
        previous = self._runtime.snapshot()
        if previous is None:
            return {"status": "error", "reason": "auto_propose runtime not available"}
        requested = patch.apply(previous)
        try:
            await self._runtime.transition_schedule(
                was_enabled=previous.enabled,
                now_enabled=requested.enabled,
            )
        except Exception as exc:  # noqa: BLE001 - domain failure becomes stable outcome
            return {
                "status": "error",
                "reason": f"failed to update scheduler: {exc}",
                "settings": previous.to_public_dict(available=True),
            }

        self._runtime.apply_live(requested)
        try:
            self._runtime.persist(requested)
        except OSError as exc:
            self._runtime.apply_live(previous)
            try:
                await self._runtime.transition_schedule(
                    was_enabled=requested.enabled,
                    now_enabled=previous.enabled,
                )
            except Exception:  # noqa: BLE001 - preserve the primary persistence failure
                pass
            return {
                "status": "error",
                "reason": f"failed to persist settings: {exc}",
                "settings": previous.to_public_dict(available=True),
            }
        return {
            "status": "ok",
            "settings": requested.to_public_dict(available=True),
        }

    @staticmethod
    def _proposal_id(value: str) -> str:
        if not isinstance(value, str) or not _PROPOSAL_ID.fullmatch(value):
            raise ValueError("proposal_id must be 8 lowercase hex chars")
        return value

    @staticmethod
    def _skill_name(value: str) -> str:
        if not isinstance(value, str) or not _SKILL_NAME.fullmatch(value):
            raise ValueError("name must be a valid skill name")
        return value


__all__ = [
    "AutoProposalRuntimePort",
    "ProposalSettings",
    "ProposalSettingsPatch",
    "SkillProposalReview",
    "SkillProposalStorePort",
]
