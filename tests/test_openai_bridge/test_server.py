"""Tests for the OpenAI-compatible HTTP bridge."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from opensquilla.openai_bridge import server as bridge_server
from opensquilla.openai_bridge.server import (
    _build_display_name,
    _build_user_message,
    _collect_terminal_error,
    _detect_client_title_request,
    _event_error_message,
    _filter_stream_delta,
    _first_user_text,
    _map_error_event,
    _message_text,
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


def test_event_error_message_extraction() -> None:
    """错误描述提取优先级：error_message > message > 兑底。"""
    assert _event_error_message({"message": "boom", "code": "x"}) == "boom"
    assert (
        _event_error_message({"error_message": "raw", "message": "terminal", "code": "x"})
        == "raw"
    )
    assert _event_error_message({"code": "abc"}) == "Agent error (abc)"
    assert _event_error_message({}) == "Agent error"


def test_map_error_event_timeout_and_generic() -> None:
    """timeout 类错误映射为 type=timeout，其余为 server_error 并透传 code。"""
    mapped = _map_error_event(
        {"message": "Stream idle timeout", "code": "stream_idle_timeout", "terminal_reason": "timeout"}
    )
    assert mapped["type"] == "timeout"
    assert mapped["code"] == "stream_idle_timeout"
    mapped = _map_error_event({"message": "llm ensemble had 3 successful proposer(s)", "code": "agent_error"})
    assert mapped["type"] == "server_error"
    assert mapped["code"] == "agent_error"
    assert "llm ensemble" in str(mapped["message"])


def test_collect_terminal_error() -> None:
    """仅失败终止事件返回错误描述；正常 done 不误报。"""
    ok = [
        {"event": "session.event.text_delta", "payload": {"text": "hi"}},
        {"event": "session.event.done", "payload": {}},
    ]
    assert _collect_terminal_error(ok) is None
    failed = [
        {"event": "session.event.text_delta", "payload": {"text": "hi"}},
        {"event": "session.event.error", "payload": {"message": "boom", "code": "x"}},
    ]
    assert _collect_terminal_error(failed) == "boom"


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


# --------------------------------------------------------------------------
# 客户端侧标题请求短路（dsh 双对话框 + 思考泄漏修复）
# --------------------------------------------------------------------------
def test_message_text_formats() -> None:
    """_message_text 兼容字符串与 content-part 数组两种形态。"""
    assert _message_text({"content": "hello"}) == "hello"
    assert (
        _message_text({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]})
        == "a\nb"
    )
    assert _message_text({"content": None}) == ""
    assert _message_text("not a dict") == ""


def test_detect_client_title_request_hits_signature() -> None:
    """dsh 风格标题请求命中双签名，返回最后一条人类消息文本。"""
    messages = [
        {
            "role": "system",
            "content": "Create a concise title for an AI coding-assistant session from the supplied human messages.",
        },
        {
            "role": "user",
            "content": 'Generate the session title from this JSON array of human messages:\n[{"seq":8,"text":"回答我：159753"}]',
        },
    ]
    assert _detect_client_title_request(messages) == "回答我：159753"


def test_detect_client_title_request_picks_last_entry() -> None:
    """多条人类消息时取最后一条非空 text。"""
    messages = [
        {"role": "system", "content": "Create a concise title for an AI coding-assistant session."},
        {
            "role": "user",
            "content": 'Generate the session title from this JSON array of human messages:\n[{"seq":1,"text":"first"},{"seq":2,"text":"second"}]',
        },
    ]
    assert _detect_client_title_request(messages) == "second"


def test_detect_client_title_request_misses_normal_chat() -> None:
    """双签名缺一即放行回正常流程，绝不误伤正常对话。"""
    # 缺 system 签名
    assert (
        _detect_client_title_request(
            [
                {
                    "role": "user",
                    "content": 'Generate the session title from this JSON array of human messages:\n[{"seq":1,"text":"hi"}]',
                }
            ]
        )
        is None
    )
    # system 签名在但 user 前缀不匹配
    assert (
        _detect_client_title_request(
            [
                {"role": "system", "content": "Create a concise title for an AI coding-assistant session."},
                {"role": "user", "content": "请正常与我对话"},
            ]
        )
        is None
    )
    # JSON 解析失败 → 回落正常流程
    assert (
        _detect_client_title_request(
            [
                {"role": "system", "content": "Create a concise title for an AI coding-assistant session."},
                {"role": "user", "content": "Generate the session title from this JSON array of human messages:\nnot a json array"},
            ]
        )
        is None
    )
    # 数组内 text 全为空白
    assert (
        _detect_client_title_request(
            [
                {"role": "system", "content": "Create a concise title for an AI coding-assistant session."},
                {"role": "user", "content": 'Generate the session title from this JSON array of human messages:\n[{"seq":1,"text":"   "}]'},
            ]
        )
        is None
    )


def test_detect_client_title_request_truncates_long_text() -> None:
    """标题截断上限 48 字符。"""
    long_text = "字" * 100
    messages = [
        {"role": "system", "content": "Create a concise title for an AI coding-assistant session."},
        {
            "role": "user",
            "content": f'Generate the session title from this JSON array of human messages:\n[{{"seq":1,"text":"{long_text}"}}]',
        },
    ]
    title = _detect_client_title_request(messages)
    assert title is not None
    assert len(title) <= 48
    assert title.startswith("字")


def test_chat_completions_short_circuits_client_title_request() -> None:
    """端点级短路：标题请求返回纯文本标题，不唤醒智能体（无 RPC 依赖）。"""
    app = create_app(no_auth=True, bridge_token="test-bridge-token")
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "OpenSquilla",
            "messages": [
                {"role": "system", "content": "Create a concise title for an AI coding-assistant session."},
                {
                    "role": "user",
                    "content": 'Generate the session title from this JSON array of human messages:\n[{"seq":8,"text":"回答我：159753"}]',
                },
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "回答我：159753"
    assert body["choices"][0]["finish_reason"] == "stop"


def test_first_user_text_extracts_first_non_empty_user() -> None:
    """首条非空 user 消息被提取，system 与空 user 被跳过。"""
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": ""},
        {"role": "user", "content": "  第一条有效消息  "},
        {"role": "user", "content": "后续消息"},
    ]
    assert _first_user_text(messages) == "  第一条有效消息  "


def test_first_user_text_supports_content_parts() -> None:
    """content 为 [{"type":"text","text":...}] 数组时仍能提取。"""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "片段一"}, {"type": "text", "text": "片段二"}]}
    ]
    assert _first_user_text(messages) == "片段一\n片段二"


def test_first_user_text_returns_none_without_user() -> None:
    """无 user 消息时返回 None。"""
    assert _first_user_text([{"role": "system", "content": "sys"}]) is None
    assert _first_user_text([]) is None


def test_build_display_name_includes_timestamp_and_snippet() -> None:
    """命名含时间前缀 + 首句摘要。"""
    name = _build_display_name("main", "解释一下相对论")
    assert name.startswith("OpenAI bridge · ")
    assert "解释一下相对论" in name


def test_build_display_name_falls_back_without_user_text() -> None:
    """无首句时退化为仅时间前缀。"""
    name = _build_display_name("main", None)
    assert name.startswith("OpenAI bridge · ")
    assert " · " in name  # 时间戳占位存在


def test_build_display_name_truncates_long_snippet() -> None:
    """首句超长截断到 24 字符并加省略号。"""
    long_text = "这是一段非常非常非常非常非常非常非常非常非常非常非常长的输入"
    name = _build_display_name("main", long_text)
    snippet = name.split(" · ")[-1]
    assert snippet.endswith("…")
    assert len(snippet) <= 25  # 24 字 + 省略号


def test_build_display_name_collapses_whitespace_and_control_chars() -> None:
    """换行/制表折叠为空格，控制字符被剔除。"""
    name = _build_display_name("main", "第一行\n第二行\t第三行\x00")
    assert "\n" not in name
    assert "\t" not in name
    assert "\x00" not in name
    assert "第一行 第二行 第三行" in name


# --------------------------------------------------------------------
# _build_user_message — include_history (失忆修复回归测试)
# --------------------------------------------------------------------

def test_build_user_message_v1_last_user_only() -> None:
    """include_history=False（默认）：v1 行为，只取最后一条 user。"""
    result = _build_user_message([
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "回答1"},
        {"role": "user", "content": "第二问"},
    ])
    # 不应包含第一问（历史），只包含第二问（当前）
    assert "第一问" not in result
    assert "第二问" in result
    # system 前缀以 v1 格式拼入
    assert "你是助手" in result


def test_build_user_message_include_history_preserves_full_history() -> None:
    """include_history=True：全量历史序列化，包含第一问。"""
    result = _build_user_message([
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "回答1"},
        {"role": "user", "content": "第二问"},
    ], include_history=True)
    assert "第一问" in result
    assert "第二问" in result
    assert "回答1" in result
    # 结构标记
    assert "[此前对话历史" in result
    assert "[当前问题]" in result


def test_build_user_message_include_history_single_turn_no_system() -> None:
    """include_history=True 但只有一轮对话：不输出历史标记。"""
    result = _build_user_message([
        {"role": "user", "content": "你好"},
    ], include_history=True)
    assert "你好" in result
    assert "此前对话历史" not in result
    assert "当前问题" not in result


def test_build_user_message_include_history_with_system() -> None:
    """include_history=True + system 前缀 → [系统设定] 标记。"""
    result = _build_user_message([
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "回答1"},
        {"role": "user", "content": "当前问"},
    ], include_history=True)
    assert "[系统设定]" in result
    assert "你是助手" in result
    assert "第一问" in result
    assert "当前问" in result


def test_build_user_message_missing_user_raises() -> None:
    """无 user 消息抛 HTTP 400。"""
    with pytest.raises(HTTPException, match="缺少 user"):
        _build_user_message([{"role": "system", "content": "你是助手"}])


def test_build_user_message_empty_messages_raises() -> None:
    """空 messages 抛 HTTP 400。"""
    with pytest.raises(HTTPException, match="缺少 user"):
        _build_user_message([])


def test_build_user_message_include_history_last_user_is_current() -> None:
    """include_history=True 时，最后一条 user 是当前问题，不在历史中。"""
    result = _build_user_message([
        {"role": "user", "content": "历史问题"},
        {"role": "assistant", "content": "历史回答"},
        {"role": "user", "content": "当前问题"},
    ], include_history=True)
    # 历史段不应包含"当前问题"
    history_start = result.find("[此前对话历史")
    history_end = result.find("[当前问题]")
    if history_start >= 0 and history_end >= 0:
        history_section = result[history_start:history_end]
        assert "当前问题" not in history_section


def test_build_user_message_include_history_tool_role() -> None:
    """include_history=True 时 tool role 被正确标注。"""
    result = _build_user_message([
        {"role": "user", "content": "查天气"},
        {"role": "assistant", "content": "调用工具"},
        {"role": "tool", "content": "晴天 25°C"},
        {"role": "user", "content": "谢谢"},
    ], include_history=True)
    assert "Tool:" in result
    assert "晴天 25°C" in result
