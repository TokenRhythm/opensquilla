"""OpenAI 兼容 HTTP 适配层：将 OpenSquilla gateway 暴露为 /v1/chat/completions。

架构：
    OpenAI 客户端 ──HTTP /v1/*──▶ 本适配层 ──WebSocket RPC──▶ OpenSquilla Gateway ──▶ Agent

设计要点：
    * 复用 OpenSquilla 自带的 GatewayRPCClient（已含 auth 握手补丁），不重复实现协议。
    * 单连接原子化执行：subscribe → send → 收事件至终止帧，避免双连接的事件竞争。
    * 流式输出直接转发 gateway 的 session.event.text_delta 增量。

环境变量：
    OPENAI_BRIDGE_HOST            监听地址（默认 127.0.0.1）
    OPENAI_BRIDGE_PORT            监听端口（默认 8787）
    OPENAI_BRIDGE_TOKEN           适配层 API Key（缺省自动生成随机值并打印到日志）
    OPENAI_BRIDGE_NO_AUTH         设为 1 时跳过适配层认证（仅限本地调试）
    OPENAI_BRIDGE_GATEWAY_URL     gateway WebSocket 地址（默认 ws://localhost:18791/ws）
    OPENAI_BRIDGE_SESSION_MODE    persistent（默认，每模型一个常驻会话，agent 有记忆）
                                  | stateless（每请求新建会话）
    OPENAI_BRIDGE_TIMEOUT         非流式响应最长等待秒数（默认 180）

请求头：
    X-OpenSquilla-Session         指定会话 key（默认 persistent key）
    X-OpenSquilla-New-Session     设为 1 强制新建会话
    X-OpenSquilla-Route           路由钉选：auto（自动路由）/ tier:c2（或裸 c2）等；
                                  仅作用于当前请求，turn 结束自动恢复自动路由
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from opensquilla.cli.gateway_rpc import default_gateway_token
from opensquilla.gateway_client import GatewayRPCClient, GatewayRPCError

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
HOST = os.environ.get("OPENAI_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPENAI_BRIDGE_PORT", "8787"))
GATEWAY_URL = os.environ.get(
    "OPENAI_BRIDGE_GATEWAY_URL", "ws://localhost:18791/ws"
)
SESSION_MODE = os.environ.get("OPENAI_BRIDGE_SESSION_MODE", "persistent").strip().lower()
TIMEOUT_S = float(os.environ.get("OPENAI_BRIDGE_TIMEOUT", "180"))
NO_AUTH = os.environ.get("OPENAI_BRIDGE_NO_AUTH", "") == "1"

# 对外模型显示名：OpenAI 客户端看到的名字（默认 OpenSquilla），内部映射回 agent id。
DISPLAY_MODEL = os.environ.get("OPENAI_BRIDGE_MODEL", "OpenSquilla").strip() or "OpenSquilla"
_MODEL_TO_AGENT = {DISPLAY_MODEL.casefold(): "main"}


def _resolve_agent_id(model: str) -> str:
    """把请求中的模型名映射为 gateway agent id（兼容 agent: 前缀）。"""
    name = str(model).removeprefix("agent:")
    return _MODEL_TO_AGENT.get(name.casefold(), name)
BRIDGE_TOKEN = os.environ.get("OPENAI_BRIDGE_TOKEN", "").strip() or (
    None if NO_AUTH else secrets.token_hex(16)
)

# 与 mcp_server/bridge.py 同源的终止事件集合
_TERMINAL_EVENTS = {
    "session.event.done",
    "session.event.error",
    "task.cancelled",
    "task.failed",
    "task.timeout",
    "task.abandoned",
}
_TEXT_DELTA = "session.event.text_delta"
_STREAM_FRAME_TIMEOUT_S = 300.0


def _normalize_event_frame(frame: dict[str, Any]) -> dict[str, Any]:
    if "event" in frame and "payload" in frame:
        return frame
    return {"event": frame.get("event"), "payload": frame.get("payload") or frame}


def _gateway_token() -> str:
    token = os.environ.get("OPENSQUILLA_GATEWAY_TOKEN", "").strip()
    if not token:
        try:
            token = default_gateway_token() or ""
        except Exception:  # noqa: BLE001 - 配置缺失时回退
            token = ""
    return token.strip()


async def _new_client() -> GatewayRPCClient:
    client = GatewayRPCClient()
    token = _gateway_token()
    await client.connect(GATEWAY_URL, auth={"token": token} if token else None)
    return client


# --------------------------------------------------------------------------
# 会话管理
# --------------------------------------------------------------------------
# persistent 模式：sessions.create 不接受自定义 key（gateway 自动生成
# ``agent:{agent_id}:{hex}``），因此把创建返回的真实 key 落盘缓存，之后
# 请求优先复用缓存 key（resolve 失败则重建）。修复 v1 "每次请求都新建
# 会话" 导致记忆丢失、孤儿会话堆积的问题。
_SESSION_CACHE_DIR = Path(__file__).resolve().parent / ".session_cache"


def _persistent_key_file(agent_id: str) -> Path:
    safe = agent_id.replace(":", "_").replace("/", "_").replace("\\", "_")
    return _SESSION_CACHE_DIR / f"{safe}.json"


def _read_cached_key(agent_id: str) -> str | None:
    try:
        data = json.loads(_persistent_key_file(agent_id).read_text(encoding="utf-8"))
        key = data.get("key") if isinstance(data, dict) else None
        return key if isinstance(key, str) and key else None
    except Exception:  # noqa: BLE001 - 缓存缺失/损坏时退回新建
        return None


def _write_cached_key(agent_id: str, key: str) -> None:
    try:
        _SESSION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _persistent_key_file(agent_id).write_text(
            json.dumps({"agent_id": agent_id, "key": key}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - 缓存写失败不阻断请求
        print(f"[openai-bridge] persistent key 缓存失败: {key}")


async def _create_session(client: GatewayRPCClient, agent_id: str) -> str:
    result = await client.call(
        "sessions.create",
        {"agentId": agent_id, "displayName": f"OpenAI bridge ({agent_id})"},
    )
    key = result.get("key") if isinstance(result, dict) else None
    if not key:
        raise RuntimeError(f"sessions.create 未返回 key: {result!r}")
    return key


async def _resolve_or_create_session(
    client: GatewayRPCClient, agent_id: str, *, explicit_key: str | None, force_new: bool
) -> str:
    if explicit_key:
        try:
            await client.resolve_session(explicit_key)
            return explicit_key
        except GatewayRPCError as exc:
            raise HTTPException(404, f"会话不存在或不可用: {exc}") from exc
    if SESSION_MODE == "stateless":
        return await _create_session(client, agent_id)
    if force_new:
        key = await _create_session(client, agent_id)
        _write_cached_key(agent_id, key)
        return key
    # persistent：优先复用缓存 key
    cached = _read_cached_key(agent_id)
    if cached:
        try:
            await client.resolve_session(cached)
            return cached
        except GatewayRPCError:
            pass  # 缓存失效（会话被删/清库），重建
    key = await _create_session(client, agent_id)
    _write_cached_key(agent_id, key)
    return key


# 同一会话同时只允许一个 turn 在途，防止事件流互相串扰
_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()


async def _get_session_lock(key: str) -> asyncio.Lock:
    async with _session_locks_guard:
        lock = _session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[key] = lock
        return lock


# --------------------------------------------------------------------------
# 消息与请求组装
# --------------------------------------------------------------------------
def _extract_text_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        parts: list[str] = []
        for seg in content:
            if isinstance(seg, dict) and seg.get("type") == "text":
                t = seg.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
        return "\n".join(parts) if parts else None
    return None


def _build_user_message(messages: list[dict[str, Any]]) -> str:
    """取最后一条 user 消息；system 消息作为前缀拼入（v1 简化策略）。"""
    prefix_parts: list[str] = []
    last_user: str | None = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        text = _extract_text_content(msg.get("content"))
        if not text:
            continue
        if role == "system":
            prefix_parts.append(text)
        elif role == "user":
            last_user = text
    if last_user is None:
        raise HTTPException(400, "messages 中缺少 user 消息")
    if prefix_parts:
        return "System:\n" + "\n\n".join(prefix_parts) + "\n\nUser:\n" + last_user
    return last_user


def _extract_final_text(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for ev in events:
        if ev.get("event") == _TEXT_DELTA:
            t = ev.get("payload", {}).get("text")
            if isinstance(t, str) and t:
                parts.append(t)
    return "".join(parts)


# --------------------------------------------------------------------------
# 路由钉选（X-OpenSquilla-Route）
# --------------------------------------------------------------------------
# 通过 gateway 的 routing.hold.set/clear RPC 实现 tier 钉选：turn 前设置
# hold（仅当前请求，turns=1），turn 结束后无论成败都恢复自动路由。
# 目标值支持 auto / tier:c2 / 裸 c2 等（routing.hold.set 内部会规范化）。
AUTO_TARGET = "auto"


async def _apply_route_hold(client: GatewayRPCClient, key: str, route: str | None) -> None:
    """Turn 前应用路由钉选；route 为空或 auto 时跳过。"""
    if not route or route.strip().lower() == AUTO_TARGET:
        return
    target = route.strip()
    try:
        await client.call(
            "routing.hold.set",
            {"sessionKey": key, "target": target, "turns": 1},
        )
    except GatewayRPCError as exc:
        # 钉选失败不阻断请求：回退自动路由并如实告警
        print(f"[openai-bridge] routing.hold.set 失败，回退自动路由: {exc}")


async def _clear_route_hold(client: GatewayRPCClient, key: str, route: str | None) -> None:
    """Turn 结束后恢复自动路由；失败仅告警不影响返回。"""
    if not route or route.strip().lower() == AUTO_TARGET:
        return
    try:
        await client.call("routing.hold.clear", {"sessionKey": key})
    except GatewayRPCError as exc:
        print(f"[openai-bridge] routing.hold.clear 失败: {exc}")


# --------------------------------------------------------------------------
# 单连接 turn 执行（subscribe → send → 收事件至终止）
# --------------------------------------------------------------------------
async def _run_turn(
    key: str, user_message: str, route: str | None = None
) -> list[dict[str, Any]]:
    client = await _new_client()
    try:
        await _apply_route_hold(client, key, route)
        await client.call(
            "sessions.messages.subscribe", {"key": key, "since_stream_seq": None}
        )
        await client.call(
            "sessions.send",
            {
                "key": key,
                "message": user_message,
                "attachments": [],
                "intent": "continue",
                "_source": {
                    "caller_kind": "cli",
                    "channel_kind": "cli",
                    "channel_id": "openai-bridge",
                    "source_kind": "openai_bridge",
                    "source_name": "openai_bridge",
                },
            },
        )
        events: list[dict[str, Any]] = []
        while True:
            frame = await client.recv_event(timeout=_STREAM_FRAME_TIMEOUT_S)
            norm = _normalize_event_frame(frame)
            payload = norm.get("payload")
            if not isinstance(payload, dict) or payload.get("session_key") != key:
                continue
            name = str(norm.get("event") or "")
            events.append({"event": name, "payload": payload})
            if name in _TERMINAL_EVENTS:
                break
        return events
    finally:
        await _clear_route_hold(client, key, route)
        await client.close()


# --------------------------------------------------------------------------
# 响应组装
# --------------------------------------------------------------------------
def _chat_completion(chat_id: str, created: int, model: str, content: str) -> dict[str, Any]:
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        # 说明：gateway 事件未暴露 token 统计，置零占位
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _chunk(chat_id: str, created: int, model: str, delta: dict[str, Any], finish: str | None) -> str:
    data = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json_dumps(data)}\n\n"


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


# --------------------------------------------------------------------------
# FastAPI 应用
# --------------------------------------------------------------------------
app = FastAPI(title="OpenSquilla OpenAI Bridge", version="0.1.0")


async def require_bridge_token(authorization: str | None = Header(None)) -> str | None:
    if NO_AUTH:
        return None
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            401,
            detail={
                "error": {
                    "message": "缺少或非法的 Authorization 头",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )
    token = authorization[len("Bearer "):].strip()
    if not BRIDGE_TOKEN or token != BRIDGE_TOKEN:
        raise HTTPException(
            401,
            detail={
                "error": {
                    "message": "API Key 无效",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )
    return token


@app.get("/v1/models")
async def list_models(_: str | None = Depends(require_bridge_token)) -> dict[str, Any]:
    # 对外只暴露显示名（默认 OpenSquilla），内部由 _resolve_agent_id 映射回 agent id。
    ids = [DISPLAY_MODEL]
    return {
        "object": "list",
        "data": [
            {"id": i, "object": "model", "created": int(time.time()), "owned_by": "opensquilla"}
            for i in ids
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    _: str | None = Depends(require_bridge_token),
) -> Any:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "请求体不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")

    model = str(body.get("model") or DISPLAY_MODEL)
    agent_id = _resolve_agent_id(model)
    stream = bool(body.get("stream", False))
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages 必须是非空数组")

    user_message = _build_user_message(messages)

    explicit_key = request.headers.get("X-OpenSquilla-Session")
    force_new = request.headers.get("X-OpenSquilla-New-Session", "") == "1"
    route = request.headers.get("X-OpenSquilla-Route")

    # 解析/创建会话 key
    client = await _new_client()
    try:
        key = await _resolve_or_create_session(
            client, agent_id, explicit_key=explicit_key, force_new=force_new
        )
    finally:
        await client.close()

    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    lock = await _get_session_lock(key)

    if stream:

        async def event_stream():
            # 锁在生成器内部持有：流被消费的整个生命周期内互斥
            async with lock:
                c = await _new_client()
                try:
                    await _apply_route_hold(c, key, route)
                    await c.call(
                        "sessions.messages.subscribe",
                        {"key": key, "since_stream_seq": None},
                    )
                    await c.call(
                        "sessions.send",
                        {
                            "key": key,
                            "message": user_message,
                            "attachments": [],
                            "intent": "continue",
                            "_source": {
                                "caller_kind": "cli",
                                "channel_kind": "cli",
                                "channel_id": "openai-bridge",
                                "source_kind": "openai_bridge",
                                "source_name": "openai_bridge",
                            },
                        },
                    )
                    yield _chunk(chat_id, created, model, {"role": "assistant"}, None)
                    while True:
                        frame = await c.recv_event(timeout=_STREAM_FRAME_TIMEOUT_S)
                        norm = _normalize_event_frame(frame)
                        payload = norm.get("payload")
                        if not isinstance(payload, dict) or payload.get("session_key") != key:
                            continue
                        name = str(norm.get("event") or "")
                        if name == _TEXT_DELTA:
                            t = payload.get("text")
                            if isinstance(t, str) and t:
                                yield _chunk(chat_id, created, model, {"content": t}, None)
                        if name in _TERMINAL_EVENTS:
                            yield _chunk(chat_id, created, model, {}, "stop")
                            yield "data: [DONE]\n\n"
                            return
                finally:
                    await _clear_route_hold(c, key, route)
                    await c.close()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # 非流式：同一会话串行执行
    async with lock:
        try:
            events = await asyncio.wait_for(
                _run_turn(key, user_message, route=route), timeout=TIMEOUT_S
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(504, f"agent 响应超时（>{TIMEOUT_S:g}s）") from exc
        content = _extract_final_text(events)
        return _chat_completion(chat_id, created, model, content)


def create_app(
    *,
    gateway_url: str | None = None,
    bridge_token: str | None = None,
    no_auth: bool | None = None,
    session_mode: str | None = None,
    display_model: str | None = None,
    timeout_s: float | None = None,
) -> FastAPI:
    """创建 FastAPI 应用（可注入配置覆盖环境变量，便于测试）。

    参数全部可选，缺省从环境变量读取。返回已注册路由的 app 实例。
    """
    global GATEWAY_URL, BRIDGE_TOKEN, NO_AUTH, SESSION_MODE, DISPLAY_MODEL, TIMEOUT_S  # noqa: PLW0603
    if gateway_url is not None:
        GATEWAY_URL = gateway_url
    if bridge_token is not None:
        BRIDGE_TOKEN = bridge_token
    if no_auth is not None:
        NO_AUTH = no_auth
    if session_mode is not None:
        SESSION_MODE = session_mode.strip().lower()
    if display_model is not None:
        DISPLAY_MODEL = display_model.strip() or "OpenSquilla"
        _MODEL_TO_AGENT.clear()
        _MODEL_TO_AGENT[DISPLAY_MODEL.casefold()] = "main"
    if timeout_s is not None:
        TIMEOUT_S = float(timeout_s)
    return app


def run_server(
    *,
    host: str | None = None,
    port: int | None = None,
    gateway_url: str | None = None,
    bridge_token: str | None = None,
    no_auth: bool | None = None,
    session_mode: str | None = None,
    display_model: str | None = None,
    timeout_s: float | None = None,
    log_level: str = "info",
) -> None:
    """启动 uvicorn HTTP 服务。"""
    create_app(
        gateway_url=gateway_url,
        bridge_token=bridge_token,
        no_auth=no_auth,
        session_mode=session_mode,
        display_model=display_model,
        timeout_s=timeout_s,
    )
    if BRIDGE_TOKEN and not NO_AUTH:
        print(f"[openai-bridge] API Key: {BRIDGE_TOKEN}")
    print(f"[openai-bridge] listening on http://{HOST}:{PORT}")
    print(f"[openai-bridge] gateway: {GATEWAY_URL} | session mode: {SESSION_MODE}")
    uvicorn.run(app, host=host or HOST, port=port or PORT, log_level=log_level)


if __name__ == "__main__":
    run_server()
