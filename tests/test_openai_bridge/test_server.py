"""Tests for the OpenAI-compatible HTTP bridge."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opensquilla.openai_bridge import server as bridge_server
from opensquilla.openai_bridge.server import create_app, _resolve_agent_id


def test_resolve_agent_id_default_mapping() -> None:
    """默认显示名 OpenSquilla 映射回 main agent，大小写不敏感，直传 main 兼容。"""
    assert _resolve_agent_id("OpenSquilla") == "main"
    assert _resolve_agent_id("opensquilla") == "main"
    assert _resolve_agent_id("main") == "main"
    assert _resolve_agent_id("agent:OpenSquilla") == "main"


def test_resolve_agent_id_custom_display_model() -> None:
    """自定义显示名后映射仍正确，且不影响直传 agent id。"""
    create_app(no_auth=True, bridge_token="sk-test", display_model="MyAgent")
    try:
        assert bridge_server._resolve_agent_id("MyAgent") == "main"
        assert bridge_server._resolve_agent_id("myagent") == "main"
        assert bridge_server._resolve_agent_id("other") == "other"
    finally:
        # 恢复默认（create_app 仅覆盖非 None 参数，必须显式传回默认值）
        create_app(no_auth=True, bridge_token="sk-test", display_model="OpenSquilla")


def test_app_routes_registered() -> None:
    """核心路由必须存在。"""
    app = create_app(no_auth=True, bridge_token="sk-test")
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/v1/models" in paths
    assert "/v1/chat/completions" in paths


def test_models_requires_auth() -> None:
    """无 Authorization 头必须 401。"""
    app = create_app(no_auth=False, bridge_token="sk-test-1234")
    client = TestClient(app)
    resp = client.get("/v1/models")
    assert resp.status_code == 401


def test_models_returns_display_model() -> None:
    """/v1/models 返回对外显示名 OpenSquilla。"""
    app = create_app(no_auth=False, bridge_token="sk-test-1234")
    client = TestClient(app)
    resp = client.get("/v1/models", headers={"Authorization": "Bearer sk-test-1234"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    ids = [m["id"] for m in data["data"]]
    assert ids == ["OpenSquilla"]


def test_chat_completions_requires_auth() -> None:
    """无 Authorization 头必须 401。"""
    app = create_app(no_auth=False, bridge_token="sk-test-1234")
    client = TestClient(app)
    resp = client.post("/v1/chat/completions", json={"model": "OpenSquilla", "messages": []})
    assert resp.status_code == 401


def test_chat_completions_validates_messages() -> None:
    """messages 缺失/为空必须 400（不触达 gateway）。"""
    app = create_app(no_auth=True, bridge_token="sk-test-1234")
    client = TestClient(app)
    headers = {"Authorization": "Bearer sk-test-1234"}

    resp = client.post("/v1/chat/completions", json={}, headers=headers)
    assert resp.status_code == 400

    resp = client.post(
        "/v1/chat/completions", json={"model": "OpenSquilla", "messages": []}, headers=headers
    )
    assert resp.status_code == 400

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "OpenSquilla", "messages": "not-a-list"},
        headers=headers,
    )
    assert resp.status_code == 400


def test_chat_completions_no_auth_mode() -> None:
    """OPENAI_BRIDGE_NO_AUTH=1 时跳过认证，请求进入业务校验。"""
    app = create_app(no_auth=True, bridge_token="sk-test-1234")
    client = TestClient(app)
    # 无 token 也允许访问（no_auth 模式），但 messages 校验仍生效
    resp = client.post(
        "/v1/chat/completions", json={"model": "OpenSquilla", "messages": []}
    )
    assert resp.status_code == 400  # 走到消息校验而非 401
