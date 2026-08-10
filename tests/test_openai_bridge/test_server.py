"""Tests for the OpenAI-compatible HTTP bridge."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opensquilla.openai_bridge import server as bridge_server
from opensquilla.openai_bridge.server import (
    _filter_stream_delta,
    _resolve_agent_id,
    _strip_reply_tags,
    create_app,
)


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


def test_strip_reply_tags_removes_internal_markers() -> None:
    """路由标记必须被剥除，且不误伤其他 [[...]] 内容。"""
    assert _strip_reply_tags("[[reply_to_current]]pong") == "pong"
    assert _strip_reply_tags("[[reply_to:msg_1]] hello") == " hello"
    assert _strip_reply_tags("[[reply_to]]x") == "x"
    assert _strip_reply_tags("no tags here") == "no tags here"
    assert _strip_reply_tags("[[bold]] kept") == "[[bold]] kept"


def test_filter_stream_delta_handles_split_marker() -> None:
    """实测切分：'[[reply_to' 与 '_current]]' 分属两个增量。"""
    out, carry = _filter_stream_delta("", "[[reply_to")
    assert (out, carry) == ("", "[[reply_to")
    out, carry = _filter_stream_delta(carry, "_current]]")
    assert (out, carry) == ("", "")
    out, carry = _filter_stream_delta(carry, "\n1\n2")
    assert (out, carry) == ("\n1\n2", "")


def test_filter_stream_delta_passthrough_cases() -> None:
    """普通文本、非标记的闭合括号、同增量内的标记均正确处理。"""
    assert _filter_stream_delta("", "plain text") == ("plain text", "")
    assert _filter_stream_delta("", "keep [[bold]]") == ("keep [[bold]]", "")
    assert _filter_stream_delta("", "a[[reply_to_current]]b") == ("ab", "")


def test_error_body_follows_openai_envelope() -> None:
    """错误响应必须是 {"error": {...}} 而非 FastAPI 的 {"detail": ...}。"""
    app = create_app(no_auth=False, bridge_token="sk-test-1234")
    client = TestClient(app)
    resp = client.get("/v1/models")
    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body and "detail" not in body
    assert body["error"]["code"] == "invalid_api_key"

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "OpenSquilla", "messages": []},
        headers={"Authorization": "Bearer sk-test-1234"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body and "detail" not in body
    assert body["error"]["type"] == "invalid_request_error"
