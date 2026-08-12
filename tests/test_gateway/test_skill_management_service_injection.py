from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

import opensquilla.gateway.app as gateway_app
from opensquilla.gateway.boot import build_services
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.protocol import make_ok_res
from opensquilla.skills.hub.transaction import journal_path_for_state
from opensquilla.skills.paths import SkillLayerDirs
from opensquilla.tools.registry import get_default_registry


class _CapturingDispatcher:
    def __init__(self) -> None:
        self.contexts: list[tuple[str, Any]] = []

    def list_methods(self) -> list[str]:
        return ["sessions.list", "injection.probe"]

    async def dispatch(self, req_id: str, method: str, params: Any, ctx: Any) -> Any:
        self.contexts.append((method, ctx))
        if method == "sessions.list":
            return make_ok_res(req_id, {"sessions": [], "count": 0})
        return make_ok_res(req_id, {"method": method})


def _write_skill(root: Path, name: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} fixture\n---\n{body}\n",
        encoding="utf-8",
    )


def test_http_and_websocket_contexts_share_skill_management_service(
    monkeypatch,
) -> None:
    dispatcher = _CapturingDispatcher()
    management_service = object()
    monkeypatch.setattr(gateway_app, "get_dispatcher", lambda: dispatcher)
    config = GatewayConfig(ws_writer_queue_enabled=False)
    app = gateway_app.create_gateway_app(
        config,
        skill_management_service=management_service,
    )

    origin = "http://127.0.0.1:18791"
    with TestClient(
        app,
        base_url=origin,
        client=("127.0.0.1", 51000),
    ) as client:
        response = client.get("/api/sessions")
        assert response.status_code == 200

        with client.websocket_connect("/ws") as ws:
            challenge = ws.receive_json()
            assert challenge["event"] == "connect.challenge"
            ws.send_json(
                {
                    "type": "req",
                    "id": "connect",
                    "method": "connect",
                    "params": {"minProtocol": 1, "role": "operator", "auth": {}},
                }
            )
            ws.receive_json()
            ws.send_json(
                {
                    "type": "req",
                    "id": "probe",
                    "method": "injection.probe",
                    "params": {},
                }
            )
            result = ws.receive_json()
            assert result["id"] == "probe"
            assert result["ok"] is True

    http_ctx = next(ctx for method, ctx in dispatcher.contexts if method == "sessions.list")
    ws_ctx = next(ctx for method, ctx in dispatcher.contexts if method == "injection.probe")
    assert http_ctx.skill_management_service is management_service
    assert ws_ctx.skill_management_service is management_service
    assert http_ctx.skill_management_service is ws_ctx.skill_management_service


@pytest.mark.parametrize(
    "management_builder_fails",
    [False, True],
    ids=["management-ready", "management-builder-fails"],
)
@pytest.mark.asyncio
async def test_gateway_boot_quarantines_only_managed_skills_when_recovery_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    management_builder_fails: bool,
) -> None:
    from opensquilla.gateway import rpc_skills
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.profile_operation_lock import ProfileOperationLock
    from opensquilla.skills import paths as skill_paths

    state_root = tmp_path / "state"
    bundled = tmp_path / "bundled"
    managed = tmp_path / "managed"
    personal = tmp_path / "personal"
    project = tmp_path / "project"
    workspace = tmp_path / "workspace-skills"
    for root, name in (
        (bundled, "bundled-safe"),
        (managed, "managed-untrusted"),
        (personal, "personal-safe"),
        (project, "project-safe"),
        (workspace, "workspace-safe"),
    ):
        _write_skill(root, name, f"{name} instructions")

    journal = journal_path_for_state(managed, state_root)
    journal.parent.mkdir(parents=True)
    journal.write_text("{truncated", encoding="utf-8")

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(state_root))
    monkeypatch.setattr(
        skill_paths,
        "resolve_skill_layer_dirs",
        lambda **_kwargs: SkillLayerDirs(
            bundled_dir=bundled,
            managed_dir=managed,
            personal_agents_dir=personal,
            project_agents_dir=project,
            workspace_dir=workspace,
        ),
    )
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.configure_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            effective=SimpleNamespace(as_dict=lambda: {})
        ),
    )
    if management_builder_fails:
        def fail_management_service(**_kwargs: object) -> None:
            raise RuntimeError("synthetic management-service construction failure")

        monkeypatch.setattr(
            "opensquilla.skills.hub.defaults.build_default_skill_management_service",
            fail_management_service,
        )

    config = GatewayConfig(
        state_dir=str(state_root),
        workspace_dir=str(tmp_path / "agent-workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
        memory={"flush_enabled": False},
        sandbox={"auto_setup": False},
    )
    profile_lease = ProfileOperationLock(state_root)
    profile_lease.acquire()
    try:
        services = await build_services(
            config=config,
            session_db_path=":memory:",
            seed_agent_workspaces=False,
        )
    except BaseException:
        profile_lease.release()
        raise
    try:
        assert services.skill_loader is not None
        if management_builder_fails:
            assert services.skill_management_service is None
        else:
            assert services.skill_management_service is not None
            assert services.skill_management_service.managed_dir == managed
            assert services.skill_management_service.journal_path == journal
        skills = {skill.name: skill for skill in services.skill_loader.load_all()}
        assert {
            "bundled-safe",
            "personal-safe",
            "project-safe",
            "workspace-safe",
        } <= skills.keys()
        assert "managed-untrusted" not in skills

        if not management_builder_fails:
            skill_list = get_default_registry().get("skill_list")
            skill_view = get_default_registry().get("skill_view")
            assert skill_list is not None
            assert skill_view is not None
            listed = await skill_list.handler()
            viewed = await skill_view.handler(name="workspace-safe", file_path=None)
            assert "workspace-safe" in listed
            assert "managed-untrusted" not in listed
            assert "workspace-safe instructions" in viewed

        doctor = await rpc_skills._handle_skills_doctor(
            None,
            RpcContext(
                conn_id="recovery-test",
                config=config,
                skill_loader=services.skill_loader,
                skill_management_service=services.skill_management_service,
                skill_management_state=services.skill_management_state,
            ),
        )
        assert doctor["ok"] is False
        assert any(
            item["code"] == "RECOVERY_REQUIRED"
            for item in doctor["diagnostics"]
        )
        assert all(
            item["code"] != "MANAGED_ROOT_UNAVAILABLE"
            for item in doctor["diagnostics"]
        )

        if not management_builder_fails:
            mutation = await services.skill_management_service.install(
                "owner/community-skill",
                "github",
            )
            assert mutation.success is False
            assert [item.code for item in mutation.diagnostics] == ["RECOVERY_REQUIRED"]
            assert [
                item.code for item in services.skill_management_service.recovery_diagnostics
            ] == ["RECOVERY_REQUIRED"]
        assert journal.read_text(encoding="utf-8") == "{truncated"
    finally:
        await services.close()
        profile_lease.release()
