from __future__ import annotations

import asyncio
from types import SimpleNamespace

from opensquilla.gateway.rpc import RpcContext, get_dispatcher


async def _list_for_surface(surface: str) -> dict:
    result = await get_dispatcher().dispatch(
        "r1",
        "commands.list_for_surface",
        {"surface": surface},
        RpcContext(conn_id="test"),
    )
    assert result.error is None, result.error
    assert result.payload is not None
    return result.payload


def test_commands_list_for_surface_accepts_legacy_web_alias() -> None:
    payload = asyncio.run(_list_for_surface("web"))

    assert payload["surface"] == "web_chat"


def test_web_catalog_includes_usage_rpc_execution() -> None:
    payload = asyncio.run(_list_for_surface("web"))
    usage = next(cmd for cmd in payload["commands"] if cmd["name"] == "/usage")

    assert usage["rpc_method"] == "usage.status"
    assert usage["execution"] == {
        "kind": "rpc",
        "action": "usage.status",
        "rpc_method": "usage.status",
    }


def test_cli_gateway_catalog_serializes_argument_choices() -> None:
    payload = asyncio.run(_list_for_surface("cli_gateway"))
    permissions = next(cmd for cmd in payload["commands"] if cmd["name"] == "/permissions")

    assert permissions["argument_choices"] == [
        {
            "value": "off",
            "description": "Clear session override; configured default resumes.",
        },
        {"value": "on", "description": "Host exec, approvals required."},
        {
            "value": "bypass",
            "description": (
                "Host exec, approvals auto-granted; sensitive paths still blocked."
            ),
        },
        {
            "value": "full",
            "description": "Host exec, approvals skipped; sensitive paths bypassed.",
        },
        {"value": "status", "description": "Show current session permissions override."},
    ]


def test_channel_catalog_serialization_omits_rpc_params() -> None:
    payload = asyncio.run(_list_for_surface("channel"))

    assert payload["surface"] == "channel"
    assert all("rpc_params" not in cmd for cmd in payload["commands"])
    assert all("rpc_params" not in cmd.get("execution", {}) for cmd in payload["commands"])
    tui_only_fields = {
        "category",
        "busy_policy",
        "presentation",
        "order",
        "visible_by_default",
        "deprecated",
    }
    assert all(tui_only_fields.isdisjoint(cmd) for cmd in payload["commands"])


def test_channel_catalog_keeps_model_list_contract_and_lexical_order() -> None:
    payload = asyncio.run(_list_for_surface("channel"))
    commands = payload["commands"]
    model = next(cmd for cmd in commands if cmd["name"] == "/model")

    assert [cmd["name"] for cmd in commands] == sorted(cmd["name"] for cmd in commands)
    assert model["usage"] == "/model [name]"
    assert model["description"] == "List available models."
    assert model["argument_choices"] == []
    assert model["execution"] == {
        "kind": "rpc",
        "action": "models.list",
        "rpc_method": "models.list",
    }


def test_cli_gateway_catalog_adds_command_contract_without_removing_old_fields() -> None:
    payload = asyncio.run(_list_for_surface("cli_gateway"))
    status = next(cmd for cmd in payload["commands"] if cmd["name"] == "/status")

    assert {
        key: status[key]
        for key in (
            "name",
            "usage",
            "description",
            "aliases",
            "argument_choices",
            "execution",
        )
    } == {
        "name": "/status",
        "usage": "/status",
        "description": "Show current session, model, and mode.",
        "aliases": ["/session"],
        "argument_choices": [],
        "execution": {"kind": "local", "action": "status.show"},
    }
    assert {
        key: status[key]
        for key in (
            "category",
            "busy_policy",
            "presentation",
            "order",
            "visible_by_default",
            "deprecated",
        )
    } == {
        "category": "query",
        "busy_policy": "immediate",
        "presentation": "panel",
        "order": 60,
        "visible_by_default": True,
        "deprecated": False,
    }


def test_cli_gateway_catalog_exposes_strategy_and_hides_compatibility_entries() -> None:
    payload = asyncio.run(_list_for_surface("cli_gateway"))
    by_name = {cmd["name"]: cmd for cmd in payload["commands"]}

    assert by_name["/strategy"]["execution"] == {
        "kind": "local",
        "action": "model.routing.strategy",
    }
    assert [choice["value"] for choice in by_name["/strategy"]["argument_choices"]] == [
        "direct",
        "router",
        "ensemble",
        "status",
    ]
    assert by_name["/model"]["description"] == "Choose or inspect the session model."
    assert by_name["/model"]["execution"] == {
        "kind": "local",
        "action": "model.select",
    }
    assert [choice["value"] for choice in by_name["/model"]["argument_choices"]] == [
        "auto",
        "status",
    ]
    assert by_name["/models"]["visible_by_default"] is False
    assert by_name["/models"]["deprecated"] is True
    assert by_name["/models"]["execution"] == {
        "kind": "local",
        "action": "models.list",
    }
    assert by_name["/forget"]["visible_by_default"] is False
    assert by_name["/forget"]["deprecated"] is True

    standalone = asyncio.run(_list_for_surface("cli_standalone"))
    assert "/strategy" not in {cmd["name"] for cmd in standalone["commands"]}


def test_dynamic_meta_choices_refresh_once_then_use_one_snapshot() -> None:
    class _Loader:
        def __init__(self) -> None:
            self.refresh_calls: list[str] = []
            self._snapshot = SimpleNamespace(
                skills=(
                    SimpleNamespace(
                        name="fresh-meta",
                        description="Fresh command choice",
                        kind="meta",
                        disable_model_invocation=False,
                    ),
                )
            )

        def refresh_if_changed(self, reason: str):
            self.refresh_calls.append(reason)

        def snapshot(self):
            return self._snapshot

        def load_all(self):
            raise AssertionError("the command catalog must use its pinned snapshot")

    loader = _Loader()

    async def _dispatch() -> dict:
        result = await get_dispatcher().dispatch(
            "r-command-refresh",
            "commands.list_for_surface",
            {"surface": "web"},
            RpcContext(conn_id="test", skill_loader=loader),
        )
        assert result.error is None, result.error
        assert result.payload is not None
        return result.payload

    payload = asyncio.run(_dispatch())
    meta = next(cmd for cmd in payload["commands"] if cmd["name"] == "/meta")

    assert loader.refresh_calls == ["rpc:commands.list_for_surface"]
    assert meta["argument_choices"] == [
        {
            "value": "fresh-meta",
            "description": "Fresh command choice",
            "status": "ready",
            "missing_bins": [],
            "missing_env": [],
            "missing_env_any": [],
            "missing_skills": [],
            "missing_capabilities": [],
        }
    ]
