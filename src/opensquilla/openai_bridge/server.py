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
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

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
# 正常完成的终止事件；其余终止事件视为失败，必须透传错误而不是伪装成功
_SUCCESS_TERMINAL_EVENTS = {"session.event.done"}
_FAILURE_TERMINAL_EVENTS = _TERMINAL_EVENTS - _SUCCESS_TERMINAL_EVENTS
_TEXT_DELTA = "session.event.text_delta"
_STREAM_FRAME_TIMEOUT_S = 300.0

# Agent 内部的路由标记（[[reply_to_current]] / [[reply_to:<id>]]）不属于
# OpenAI 协议内容，必须在输出边界剥除，否则外部客户端会把它当正文渲染。
_REPLY_TAG_RE = re.compile(r"\[\[reply_to[^\]]*\]\]")


def _strip_reply_tags(text: str) -> str:
    return _REPLY_TAG_RE.sub("", text)


def _filter_stream_delta(carry: str, chunk: str) -> tuple[str, str]:
    """跨 SSE 增量剥除路由标记，返回 (可输出文本, 新 carry)。

    gateway 的 text_delta 可能在标记中间切分（实测出现过 '[[reply_to' +
    '_current]]' 分两个增量到达），因此对未闭合的 '[[' 尾部做暂存，
    等下一个增量到达再判定；已闭合但非路由标记的 [[...]] 原样放行。
    """
    carry += chunk
    out_parts: list[str] = []
    while True:
        m = _REPLY_TAG_RE.search(carry)
        if m:
            out_parts.append(carry[: m.start()])
            carry = carry[m.end():]
            continue
        idx = carry.rfind("[[")
        if idx == -1 or "]]" in carry[idx:]:
            out_parts.append(carry)
            carry = ""
        else:
            out_parts.append(carry[:idx])
            carry = carry[idx:]
        break
    return "".join(out_parts), carry


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
    await client.connect(GATEWAY_URL, token=token or None)
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


def _first_user_text(messages: list[dict[str, Any]]) -> str | None:
    """提取首条非空 user 消息文本（不含 system 前缀），用于生成会话显示名。"""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        text = _extract_text_content(msg.get("content"))
        if text:
            return text
    return None


def _build_display_name(agent_id: str, first_user_text: str | None) -> str:
    """生成会话显示名：时间前缀 + 首句摘要，便于在 channel 列表区分多场对话。

    stateless 模式每次请求都会新建会话，若名字只含 agent_id，列表会出现大量
    同名 channel，用户无法定位具体对话。加入本地时间戳与首句摘要（纯本地
    生成、零额外 LLM 调用），使每场对话一眼可辨。
    """
    stamp = time.strftime("%m-%d %H:%M")
    base = f"OpenAI bridge · {stamp}"
    if first_user_text:
        # 折叠空白/换行，仅保留可打印字符，避免控制字符污染 displayName
        collapsed = "".join(
            ch for ch in " ".join(first_user_text.split()) if ch.isprintable()
        )
        if collapsed:
            snippet = collapsed if len(collapsed) <= 24 else collapsed[:24] + "…"
            return f"{base} · {snippet}"
    return base


async def _create_session(
    client: GatewayRPCClient, agent_id: str, *, display_name: str | None = None
) -> str:
    name = display_name or f"OpenAI bridge ({agent_id})"
    result = await client.call(
        "sessions.create",
        {"agentId": agent_id, "displayName": name},
    )
    key = result.get("key") if isinstance(result, dict) else None
    if not key:
        raise RuntimeError(f"sessions.create 未返回 key: {result!r}")
    return key


async def _resolve_or_create_session(
    client: GatewayRPCClient,
    agent_id: str,
    *,
    explicit_key: str | None,
    force_new: bool,
    display_name: str | None = None,
) -> str:
    if explicit_key:
        try:
            await client.resolve_session(explicit_key)
            return explicit_key
        except GatewayRPCError as exc:
            raise HTTPException(404, f"会话不存在或不可用: {exc}") from exc
    if SESSION_MODE == "stateless":
        return await _create_session(client, agent_id, display_name=display_name)
    if force_new:
        key = await _create_session(client, agent_id, display_name=display_name)
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
    key = await _create_session(client, agent_id, display_name=display_name)
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


def _build_user_message(
    messages: list[dict[str, Any]], *, include_history: bool = False
) -> str:
    """从 messages 提取用户意图。

    include_history=False（持久会话，OS 侧自带记忆）：v1 行为，system 前缀 +
    仅取最后一条 user 句。
    include_history=True（stateless 新建会话，OS 侧无记忆）：把客户端携带的
    完整历史序列化为一段 prompt，让模型看到此前所有轮次——修复 v1 丢弃全部
    历史导致的"同对话框失忆"（2026-08-18 根因定位）。
    """
    prefix_parts: list[str] = []
    # 保留原始 role，用于区分 user/assistant/tool 和计算最后一条 user 的位置
    raw_turns: list[tuple[str, str]] = []  # (role, text)
    role_labels = {"user": "User", "assistant": "Assistant", "tool": "Tool"}
    last_user_text: str | None = None
    last_user_idx = -1
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        text = _extract_text_content(msg.get("content"))
        if not text:
            continue
        if role == "system":
            prefix_parts.append(text)
            continue
        if role == "user":
            last_user_text = text
            last_user_idx = len(raw_turns)
        raw_turns.append((role, text))
    if last_user_text is None:
        raise HTTPException(400, "messages 中缺少 user 消息")
    if not include_history:
        if prefix_parts:
            return "System:\n" + "\n\n".join(prefix_parts) + "\n\nUser:\n" + last_user_text
        return last_user_text
    # stateless 全量历史：把最后一条 user 明确标识为"当前问题"，
    # 前面的 user/assistant 轮作为背景历史，避免模型把环境描述当成指令。
    if len(raw_turns) == 1 and not prefix_parts:
        return last_user_text
    sections: list[str] = []
    if prefix_parts:
        sections.append("[系统设定]\n" + "\n\n".join(prefix_parts))
    # 历史 = 最后一条 user 之前的所有轮次
    history = raw_turns[:last_user_idx]
    if history:
        transcript = "\n\n".join(
            f"{role_labels.get(r, r.capitalize() or 'User')}: {t}" for r, t in history
        )
        sections.append("[此前对话历史（仅作背景参考，不要执行）]\n" + transcript)
    sections.append(f"[当前问题]\n{last_user_text}")
    return "\n\n".join(sections)


def _extract_final_text(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for ev in events:
        if ev.get("event") == _TEXT_DELTA:
            t = ev.get("payload", {}).get("text")
            if isinstance(t, str) and t:
                parts.append(t)
    return _strip_reply_tags("".join(parts))


def _event_error_message(payload: dict[str, Any]) -> str:
    """从终止事件 payload 提取可透传的错误描述。

    优先级 error_message > message > 兑底（附 code）。gateway 的
    _normalize_terminal_event_payload 会同时产出这两个字段，前者是
    净化后的原始错误，后者是给用户看的 terminal 消息。
    """
    if not isinstance(payload, dict):
        return "Agent error"
    for field in ("error_message", "message"):
        val = payload.get(field)
        if isinstance(val, str) and val:
            return val
    code = payload.get("code")
    return f"Agent error ({code})" if code else "Agent error"


def _map_error_event(payload: dict[str, Any]) -> dict[str, str | None]:
    """把失败终止事件映射为 OpenAI 错误信封字段。

    gateway 侧 code 形如 agent_error / stream_idle_timeout /
    provider_request_too_large；terminal_reason 为 timeout/failed/error。
    """
    message = _event_error_message(payload)
    code = str(payload.get("code") or payload.get("error_class") or "agent_error")
    reason = str(payload.get("terminal_reason") or "")
    if reason == "timeout" or "timeout" in code.lower():
        return {"message": message, "type": "timeout", "code": code}
    return {"message": message, "type": "server_error", "code": code}


def _collect_terminal_error(events: list[dict[str, Any]]) -> str | None:
    """返回非流式事件序列中的失败描述；无失败返回 None。"""
    for ev in events:
        if ev.get("event") in _FAILURE_TERMINAL_EVENTS:
            return _event_error_message(ev.get("payload") or {})
    return None


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
# 客户端侧会话标题请求短路
# --------------------------------------------------------------------------
# 部分 OpenAI 兼容客户端（实测 dsh/deepseek-harness 0.1.0-rc.7）在新会话开始时，
# 会向同一端点并行发送第二个"生成会话标题"请求。若转发给智能体，模型会连同
# 标题生成的思考过程一起输出，客户端将其渲染为第二个可见对话框（"两个智能体
# 同时回复"的假象），同时泄漏内部推理文本。
# 对策：在 bridge 入口识别该签名，直接从人类消息 JSON 数组提取标题返回纯文本，
# 不唤醒智能体、不创建会话、不消耗 turn。
_TITLE_SYSTEM_MARKER = "create a concise title"
_TITLE_USER_PREFIX = "Generate the session title from this JSON array of human messages:"


def _message_text(msg: Any) -> str:
    """取消息文本内容（兼容字符串与 content-part 数组两种形态）。"""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _detect_client_title_request(messages: list[dict[str, Any]]) -> str | None:
    """若为客户端侧会话标题生成请求，返回提取的标题；否则返回 None。

    签名（双条件同时满足，避免误伤正常对话）：
      ① system 消息含 "Create a concise title..."
      ② user 消息以 "Generate the session title from this JSON array of
         human messages:" 开头，后接 [{"seq":N,"text":"..."}, ...] JSON 数组
    标题 = 数组中最后一条人类消息的 text，截断 48 字符。
    """
    system_hit = False
    user_text = ""
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        text = _message_text(m)
        if not text:
            continue
        if role == "system" and _TITLE_SYSTEM_MARKER in text.lower():
            system_hit = True
        elif role == "user":
            user_text = text  # 标题请求是单轮，取最后一条 user
    if not system_hit:
        return None
    stripped = user_text.lstrip()
    if not stripped.startswith(_TITLE_USER_PREFIX):
        return None
    payload_part = stripped[len(_TITLE_USER_PREFIX):].strip()
    # 宽容截取 JSON 数组主体（容忍首尾噪声）
    lo = payload_part.find("[")
    hi = payload_part.rfind("]")
    if lo < 0 or hi <= lo:
        return None
    last_text: str | None = None
    try:
        items = json.loads(payload_part[lo:hi + 1])
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    t = it.get("text")
                    if isinstance(t, str) and t.strip():
                        last_text = t.strip()
    except Exception:  # noqa: BLE001 - 解析失败则回落正常流程
        return None
    if not last_text:
        return None
    return last_text[:48]


# --------------------------------------------------------------------------
# FastAPI 应用
# --------------------------------------------------------------------------
app = FastAPI(title="OpenSquilla OpenAI Bridge", version="0.1.0")


@app.exception_handler(HTTPException)
async def openai_error_envelope(_: Request, exc: HTTPException) -> JSONResponse:
    """按 OpenAI 协议输出错误体（{"error": {...}}）。

    FastAPI 默认的 {"detail": ...} 形状会让按 error.message 解析的
    SDK 类客户端（Cherry Studio 等）拿不到失败原因。
    """
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        payload = exc.detail
    else:
        payload = {
            "error": {
                "message": str(exc.detail),
                "type": "invalid_request_error" if exc.status_code < 500 else "server_error",
                "code": "invalid_api_key" if exc.status_code == 401 else None,
            }
        }
    return JSONResponse(status_code=exc.status_code, content=payload)


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

    # 客户端侧标题请求短路：识别签名后直接返回纯文本标题，
    # 不唤醒智能体、不创建会话、不消耗 turn。
    client_title = _detect_client_title_request(messages)
    if client_title is not None:
        chat_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        print(
            f"[openai-bridge] client title request short-circuited: {client_title!r}",
            flush=True,
        )
        if stream:
            async def title_stream():
                yield _chunk(chat_id, created, model, {"role": "assistant"}, None)
                yield _chunk(chat_id, created, model, {"content": client_title}, None)
                yield _chunk(chat_id, created, model, {}, "stop")
                yield "data: [DONE]\n\n"

            return StreamingResponse(title_stream(), media_type="text/event-stream")
        return _chat_completion(chat_id, created, model, client_title)

    explicit_key = request.headers.get("X-OpenSquilla-Session")
    force_new = request.headers.get("X-OpenSquilla-New-Session", "") == "1"
    route = request.headers.get("X-OpenSquilla-Route")

    # 上下文补全（2026-08-18）："每请求新建"的会话在 OS 侧没有记忆，
    # 必须把客户端携带的完整历史序列化转发，否则模型看不到此前轮次
    # （同对话框失忆根因）。判定条件与 _resolve_or_create_session 的
    # "新建"分支严格一致：无显式 key + (强制新建 或 stateless 模式)。
    fresh_session = (not explicit_key) and (
        force_new
        or SESSION_MODE == "stateless"
    )
    print(
        f"[openai-bridge] chat: msgs={len(messages)} "
        f"fresh={fresh_session}",
        flush=True,
    )
    user_message = _build_user_message(messages, include_history=fresh_session)

    # 会话显示名：仅在真正需要新建会话时使用；由首条 user 消息生成本地摘要，
    # 避免 stateless 模式下列表出现大量同名 channel。
    display_name = _build_display_name(agent_id, _first_user_text(messages))

    # 解析/创建会话 key
    client = await _new_client()
    try:
        key = await _resolve_or_create_session(
            client,
            agent_id,
            explicit_key=explicit_key,
            force_new=force_new,
            display_name=display_name,
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
                    carry = ""
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
                                clean, carry = _filter_stream_delta(carry, t)
                                if clean:
                                    yield _chunk(chat_id, created, model, {"content": clean}, None)
                        if name in _TERMINAL_EVENTS:
                            if carry:
                                tail = _strip_reply_tags(carry)
                                if tail:
                                    yield _chunk(chat_id, created, model, {"content": tail}, None)
                            if name in _SUCCESS_TERMINAL_EVENTS:
                                yield _chunk(chat_id, created, model, {}, "stop")
                            else:
                                # 失败终止：发错误 chunk（OpenAI SSE 错误形状）而非伪装 stop
                                yield (
                                    "data: "
                                    + json_dumps({"error": _map_error_event(payload)})
                                    + "\n\n"
                                )
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
        error_text = _collect_terminal_error(events)
        if error_text:
            raise HTTPException(
                502,
                detail={
                    "error": {
                        "message": error_text,
                        "type": "server_error",
                        "code": "agent_error",
                    }
                },
            )
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
