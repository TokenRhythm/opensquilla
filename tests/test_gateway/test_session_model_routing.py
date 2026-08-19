from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.model_routing import (
    capture_model_routing_config,
    model_routing_snapshot,
)
from opensquilla.gateway.session_model_routing import (
    accepted_model_routing_audit,
    accepted_model_routing_stream,
    capture_accepted_model_routing_config,
)


@pytest.mark.asyncio
async def test_interactive_session_resolution_overlays_global_without_mutating_it() -> None:
    config = GatewayConfig(
        squilla_router={"enabled": True, "rollout_phase": "full"},
        llm_ensemble={"enabled": False},
    )
    calls: list[tuple[str, str]] = []

    class SessionManager:
        async def get_session_routing(
            self,
            session_key: str,
            *,
            fallback_mode: str,
        ) -> dict[str, Any]:
            calls.append((session_key, fallback_mode))
            return {
                "mode": "direct",
                "revision": 7,
                "source": "session_override",
            }

    accepted = await capture_accepted_model_routing_config(
        config,
        SessionManager(),
        session_key="agent:main:web:session-routing",
        run_kind="web_turn",
    )

    assert calls == [("agent:main:web:session-routing", "router")]
    assert model_routing_snapshot(accepted)["mode"] == "direct"
    assert model_routing_snapshot(config)["mode"] == "router"
    assert accepted_model_routing_audit(accepted, run_kind="web_turn") == {
        "scope": "session",
        "session_mode": "direct",
        "session_revision": 7,
        "source": "session_override",
        "effective_mode": "direct",
        "router_enabled": False,
        "ensemble_enabled": False,
        "rollout_phase": "observe",
        "selection_mode": "static_openrouter_b5",
        "run_kind": "web_turn",
    }


@pytest.mark.asyncio
async def test_legacy_default_resolution_is_audited_with_its_revision() -> None:
    config = GatewayConfig(
        squilla_router={"enabled": True, "rollout_phase": "full"},
    )

    class SessionManager:
        async def get_session_routing(
            self,
            _session_key: str,
            *,
            fallback_mode: str,
        ) -> dict[str, Any]:
            return {
                "mode": fallback_mode,
                "revision": 3,
                "initialized": True,
            }

    accepted = await capture_accepted_model_routing_config(
        config,
        SessionManager(),
        session_key="agent:main:legacy-routing",
        run_kind="session_turn",
    )

    audit = accepted_model_routing_audit(accepted, run_kind="session_turn")
    assert audit is not None
    assert audit["session_mode"] == "router"
    assert audit["session_revision"] == 3
    assert audit["source"] == "session_default_initialized"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_kind",
    ["default", "runtime_send", "cron_turn", "heartbeat", "memory_repair", "goal"],
)
async def test_noninteractive_runs_keep_global_mode_without_reading_session(
    run_kind: str,
) -> None:
    config = GatewayConfig(
        squilla_router={"enabled": True, "rollout_phase": "full"},
    )

    class SessionManager:
        async def get_session_routing(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("background work must not resolve a session mode")

    accepted = await capture_accepted_model_routing_config(
        config,
        SessionManager(),
        session_key="agent:main:background-routing",
        run_kind=run_kind,
    )

    assert model_routing_snapshot(accepted)["mode"] == "router"
    audit = accepted_model_routing_audit(accepted, run_kind=run_kind)
    assert audit is not None
    assert audit["scope"] == "global"
    assert audit["session_mode"] is None
    assert audit["session_revision"] is None
    assert audit["source"] == "global_policy"


@pytest.mark.asyncio
async def test_accepted_stream_scope_stays_active_during_direct_iteration() -> None:
    live_config = GatewayConfig(
        squilla_router={"enabled": True, "rollout_phase": "full"},
    )
    accepted = capture_model_routing_config(live_config, session_mode="direct")
    runner = TurnRunner.__new__(TurnRunner)
    runner._config = live_config
    observed: list[str] = []

    async def source():
        observed.append(model_routing_snapshot(runner._turn_config())["mode"])
        yield SimpleNamespace(kind="done")

    async for _event in accepted_model_routing_stream(source(), accepted):
        pass

    assert observed == ["direct"]
