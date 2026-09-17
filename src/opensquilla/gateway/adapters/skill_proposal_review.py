"""Gateway adapters for Skill proposal review use cases."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from opensquilla.application.skill_proposal_review import (
    AutoEnabledSkillListResult,
    AutoProposalRuntimePort,
    ProposalListResult,
    ProposalOperationResult,
    ProposalSettings,
    ProposalSettingsPatch,
    SkillProposalReview,
    SkillProposalStorePort,
)
from opensquilla.gateway.auto_propose_bridge import AutoProposeRuntime
from opensquilla.skills import proposals_lib


class FilesystemSkillProposalStore(SkillProposalStorePort):
    def __init__(
        self,
        home: Path,
        *,
        invalidate_catalog: Callable[[], None],
    ) -> None:
        self._home = home
        self._invalidate_catalog = invalidate_catalog

    async def list_proposals(self) -> ProposalListResult:
        return cast(ProposalListResult, proposals_lib.list_proposals(self._home))

    async def show_proposal(self, proposal_id: str) -> ProposalOperationResult:
        return cast(
            ProposalOperationResult,
            proposals_lib.show_proposal(self._home, proposal_id),
        )

    async def accept_proposal(
        self, proposal_id: str, *, force: bool
    ) -> ProposalOperationResult:
        return cast(
            ProposalOperationResult,
            proposals_lib.accept_proposal(self._home, proposal_id, force=force),
        )

    async def reject_proposal(self, proposal_id: str) -> ProposalOperationResult:
        return cast(
            ProposalOperationResult,
            proposals_lib.reject_proposal(self._home, proposal_id),
        )

    async def list_auto_enabled(self) -> AutoEnabledSkillListResult:
        return cast(
            AutoEnabledSkillListResult,
            proposals_lib.list_auto_enabled_skills(self._home),
        )

    async def disable_auto_enabled(self, name: str) -> ProposalOperationResult:
        return cast(
            ProposalOperationResult,
            proposals_lib.disable_auto_enabled_skill(self._home, name),
        )

    def invalidate_catalog(self) -> None:
        self._invalidate_catalog()


class GatewayAutoProposalRuntime(AutoProposalRuntimePort):
    def __init__(self, runtime: AutoProposeRuntime | None) -> None:
        self._runtime = runtime

    def snapshot(self) -> ProposalSettings | None:
        if self._runtime is None:
            return None
        config = self._runtime.config
        return ProposalSettings(
            enabled=bool(getattr(config, "enabled", False)),
            on_dream_complete=bool(getattr(config, "on_dream_complete", False)),
            auto_enable=bool(getattr(config, "auto_enable", False)),
            auto_enable_max_risk=str(
                getattr(config, "auto_enable_max_risk", "low")
            ),
            cron=str(getattr(config, "cron", "0 5 * * *")),
            window_days=int(getattr(config, "window_days", 30)),
            min_freq=int(getattr(config, "min_freq", 3)),
            top_k=int(getattr(config, "top_k", 5)),
        )

    async def transition_schedule(self, *, was_enabled: bool, now_enabled: bool) -> None:
        if self._runtime is None or was_enabled == now_enabled:
            return
        if now_enabled:
            await self._runtime.register_crons()
        else:
            await self._runtime.pause_crons()

    def apply_live(self, settings: ProposalSettings) -> None:
        if self._runtime is None:
            raise RuntimeError("auto_propose runtime not available")
        for name in (
            "enabled",
            "on_dream_complete",
            "auto_enable",
            "auto_enable_max_risk",
        ):
            setattr(self._runtime.config, name, getattr(settings, name))

    def persist(self, settings: ProposalSettings) -> None:
        if self._runtime is None:
            raise RuntimeError("auto_propose runtime not available")
        proposals_lib.write_auto_propose_settings(
            self._runtime.home,
            {
                "enabled": settings.enabled,
                "on_dream_complete": settings.on_dream_complete,
                "auto_enable": settings.auto_enable,
                "auto_enable_max_risk": settings.auto_enable_max_risk,
            },
        )


class GatewaySkillProposalReviewAdapter:
    def __init__(
        self,
        proposals: SkillProposalStorePort,
        runtime: AutoProposalRuntimePort,
    ) -> None:
        self._application = SkillProposalReview(proposals, runtime)

    async def list_proposals(self, params: dict[str, Any] | None) -> dict[str, Any]:
        del params
        return dict(await self._application.list_proposals())

    async def show_proposal(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._application.show_proposal(self._proposal_id(params)))

    async def accept_proposal(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        force = raw.get("force", False)
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        return dict(
            await self._application.accept_proposal(
                self._proposal_id(raw), force=force
            )
        )

    async def reject_proposal(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._application.reject_proposal(self._proposal_id(params)))

    async def list_auto_enabled(
        self, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        del params
        return dict(await self._application.list_auto_enabled())

    async def disable_auto_enabled(
        self, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        name = raw.get("name")
        if not isinstance(name, str):
            raise ValueError("name must be a valid skill name")
        return dict(await self._application.disable_auto_enabled(name))

    async def settings(self, params: dict[str, Any] | None) -> dict[str, Any]:
        del params
        return dict(self._application.settings())

    async def update_settings(
        self, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("params object required")
        return dict(
            await self._application.update_settings(
                ProposalSettingsPatch(
                    enabled=self._optional_bool(params, "enabled"),
                    on_dream_complete=self._optional_bool(
                        params, "on_dream_complete"
                    ),
                    auto_enable=self._optional_bool(params, "auto_enable"),
                    auto_enable_max_risk=self._optional_text(
                        params, "auto_enable_max_risk"
                    ),
                )
            )
        )

    @staticmethod
    def _proposal_id(params: Mapping[str, Any] | None) -> str:
        raw = params or {}
        values = [raw[name] for name in ("proposal_id", "proposalId") if name in raw]
        if not values or any(not isinstance(value, str) for value in values):
            raise ValueError("proposal_id must be 8 lowercase hex chars")
        normalized = cast(list[str], values)
        if len(set(normalized)) != 1:
            raise ValueError("proposal_id aliases must match")
        return normalized[0]

    @staticmethod
    def _optional_bool(params: Mapping[str, Any], name: str) -> bool | None:
        if name not in params:
            return None
        value = params[name]
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        return value

    @staticmethod
    def _optional_text(params: Mapping[str, Any], name: str) -> str | None:
        if name not in params:
            return None
        value = params[name]
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        return value


__all__ = [
    "FilesystemSkillProposalStore",
    "GatewayAutoProposalRuntime",
    "GatewaySkillProposalReviewAdapter",
]
