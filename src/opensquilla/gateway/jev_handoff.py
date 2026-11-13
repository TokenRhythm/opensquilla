"""Create an explicitly accepted, summarized continuation of an idle chat."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from typing import Any, cast
from urllib.parse import quote

from opensquilla.gateway.compaction_target import (
    build_gateway_compaction_budget,
    limit_gateway_consumer_budget,
    resolve_gateway_compaction_target,
    resolve_gateway_consumer_budget,
)
from opensquilla.gateway.rpc.registry import RpcHandlerError
from opensquilla.gateway.session_services import get_session_lock, get_session_storage
from opensquilla.jev_service import get_jev_service
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.attachment_manifest import extract_attachment_occurrences
from opensquilla.session.compaction import (
    CompactionRequest,
    build_compaction_config_from_provider,
    compact_context,
    compaction_replay_summary,
    consumer_admission_accepts,
)
from opensquilla.session.manager import (
    CompactionSourceSnapshot,
    _compaction_entry_payloads,
    _settle_workspace_operation,
)
from opensquilla.session.models import SessionNode, SessionStatus
from opensquilla.session.storage import StaleEpochError
from opensquilla.session_key import canonicalize_session_key

_ORIGIN_KEY = "jev_handoff"
_TIMEOUT_SECONDS = 60
_SUMMARY_HISTORY_TOKENS = 8192
_MEDIA_TYPES = frozenset({
    "attachment_ref", "image", "image_url", "input_image", "audio", "input_audio", "video",
})
_INSTRUCTIONS = (
    "Create a concise handoff for a new conversation, in the user's language. "
    "Retain the current goal, completed work and verified key facts, important tool results, "
    "user constraints and preferences, exact file/artifact references, unfinished work and "
    "unresolved questions. Distinguish verified results from proposals. Do not execute tasks "
    "or invent missing facts. The old conversation remains available separately; the handoff "
    "must accurately describe its limitations."
)
_INHERITED_FIELDS = (
    "model", "model_provider", "provider_override", "model_override", "auth_profile_override",
    "auth_profile_override_source", "model_routing_mode", "channel", "chat_type",
    "workspace_id", "execution_workspace", "thinking_level", "fast_mode", "verbose_level",
    "reasoning_level", "send_policy", "queue_mode", "collaboration_mode",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(),
    ).hexdigest()


def _handoff_identity(source: SessionNode, decision_id: str) -> tuple[str, str, dict[str, Any]]:
    binding = {
        "version": 1,
        "sourceSessionKey": source.session_key,
        "sourceSessionId": source.session_id,
        "sourceEpoch": source.epoch,
        "decisionDigest": _digest(decision_id),
    }
    identity = _digest(binding)
    parts = source.session_key.split(":")
    # Keep guest chats inside their existing owner namespace.
    prefix = (
        ":".join(parts[:-1])
        if len(parts) == 6 and parts[2:4] == ["webchat", "guest"]
        else f"agent:{source.agent_id}:webchat"
    )
    return f"{prefix}:jev-{identity[:32]}", f"jev-handoff-{identity}", binding


def _source_fingerprint(snapshot: CompactionSourceSnapshot) -> str:
    return _digest([snapshot.preimage, snapshot.context_fingerprint])


def _contains_media(value: Any) -> bool:
    if isinstance(value, dict):
        if any(
            isinstance(value.get(key), str) and value[key] in _MEDIA_TYPES
            for key in ("type", "kind")
        ):
            return True
        if value.get("attachments"):
            return True
        return any(_contains_media(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_contains_media(item) for item in value)
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return False
        return _contains_media(parsed)
    return False


async def _require_idle(ctx: Any, session_key: str) -> None:
    runtime = getattr(ctx, "task_runtime", None)
    has_work = getattr(runtime, "has_session_work", None)
    if not callable(has_work):
        raise RpcHandlerError("UNAVAILABLE", "暂时无法确认会话状态，请稍后重试。")
    storage = get_session_storage(ctx.session_manager)
    if await has_work(session_key) or (
        storage is not None and await storage.list_pending_chat_inputs(session_key)
    ):
        raise RpcHandlerError("CONFLICT", "请等待当前任务和待发送消息处理完，再创建摘要续聊。")


async def _require_accepted(ctx: Any, session_key: str, decision_id: str) -> None:
    service = get_jev_service(ctx.config, ctx.session_manager, ctx.turn_runner)
    decision = await service.get_decision(session_key, decision_id)
    if (
        decision is None or decision.get("status") != "accepted"
        or decision.get("action") != "new_session"
    ):
        raise RpcHandlerError("CONFLICT", "这条摘要续聊建议已失效，请重新确认。")


def _validate_child(child: SessionNode, binding: dict[str, Any]) -> dict[str, Any]:
    metadata = (child.origin or {}).get(_ORIGIN_KEY)
    if (
        child.parent_session_key != binding["sourceSessionKey"]
        or not isinstance(metadata, dict)
        or any(metadata.get(key) != value for key, value in binding.items())
    ):
        raise RpcHandlerError("CONFLICT", "续聊会话的来源校验失败，请重新发起建议。")
    return metadata


async def _completed_child(
    manager: Any, target_key: str, seed_id: str, binding: dict[str, Any],
) -> SessionNode | None:
    child = await manager.get_session(target_key)
    if child is None:
        return None
    metadata = _validate_child(child, binding)
    entries = await manager.get_transcript(
        target_key, expected_session_id=child.session_id, expected_session_epoch=child.epoch,
    )
    seed = next((entry for entry in entries if entry.message_id == seed_id), None)
    if seed is not None:
        if (
            seed.role != "assistant" or not seed.content
            or _digest(seed.content) != metadata.get("seedDigest")
        ):
            raise RpcHandlerError("CONFLICT", "续聊摘要发生变化，请打开原会话核对。")
        return cast(SessionNode, child)
    if entries:
        raise RpcHandlerError("CONFLICT", "续聊会话已产生新内容，请打开该会话核对。")
    return None


async def _require_text_only(
    ctx: Any, source: SessionNode, snapshot: CompactionSourceSnapshot,
) -> None:
    storage = get_session_storage(ctx.session_manager)
    assert storage is not None
    manifests = await storage.get_context_states(
        source.session_key, state_kind="attachment_manifest_v1",
        expected_session_id=source.session_id, expected_session_epoch=source.epoch,
    )
    canonical = await ctx.session_manager.get_canonical_transcript(
        source.session_key, expected_session_id=source.session_id,
        expected_session_epoch=source.epoch,
    )
    if (
        snapshot.previous_attachment_paths
        or manifests
        or extract_attachment_occurrences(canonical, session_id=source.session_id)
        or any(
            _contains_media([entry.content, entry.tool_calls, entry.assistant_replay])
            for entry in canonical
        )
    ):
        raise RpcHandlerError(
            "UNAVAILABLE", "当前摘要续聊暂不支持带附件的会话，请继续使用原会话；原内容未更改。",
            details={"reason": "handoff_attachments_unsupported"},
        )


async def _summarize(ctx: Any, source: SessionNode, snapshot: CompactionSourceSnapshot) -> str:
    consumer = resolve_gateway_consumer_budget(ctx, source)
    if consumer.blocked_reason or consumer.provider is None:
        raise RpcHandlerError("UNAVAILABLE", "当前模型暂时无法生成续聊摘要，请稍后重试。")
    consumer = limit_gateway_consumer_budget(
        consumer, min(_SUMMARY_HISTORY_TOKENS, max(1, consumer.context_window_tokens // 4)),
    )
    target = resolve_gateway_compaction_target(ctx, source, consumer_budget=consumer)
    config = build_compaction_config_from_provider(
        target.provider, compaction_plan=target.plan,
        compaction_config=getattr(ctx.config, "compaction", None),
    )
    if target.blocked_reason or config.llm_plan is None:
        raise RpcHandlerError("UNAVAILABLE", "当前模型暂时无法生成续聊摘要，请稍后重试。")
    config.protected_recent_messages = 0
    config.protect_semantic_tail = False
    config.protect_profile_tail = False
    config.preserve_historical_images = False
    config.total_timeout_seconds = min(config.total_timeout_seconds, _TIMEOUT_SECONDS)
    config.budget = build_gateway_compaction_budget(consumer, retained_tail_messages=0)
    entries = _compaction_entry_payloads(list(snapshot.entries))
    result = await compact_context(CompactionRequest(
        session_id=source.session_id,
        entries=entries,
        config=config,
        context_window_tokens=consumer.context_window_tokens,
        context_window_chars=config.budget.history_capacity_chars,
        custom_instructions=_INSTRUCTIONS,
        previous_summary=snapshot.previous_summary or None,
        previous_coverage_checkpoints=snapshot.previous_coverage_checkpoints,
        coverage_session_id=snapshot.coverage_session_id,
        forced_prefix_cut=len(entries) if entries else None,
        force=True,
        consumer_admission=config.budget.consumer_admission,
    ))
    summary = compaction_replay_summary(result)
    if (
        result.summary_source != "llm" or not result.summary.strip() or not summary.strip()
        or result.skip_reason or result.kept_entries or result.removed_count != len(entries)
        or not result.quality_report.get("passes_structural_gate")
        or result.summary_format != "structured_v1"
    ):
        raise RpcHandlerError(
            "UNAVAILABLE", "未能生成完整、可用的续聊摘要，原会话已保留，请稍后重试。",
            details={"reason": "handoff_summary_unavailable"},
        )
    source_url = f"/control/chat?session={quote(source.session_key, safe='')}"
    seed = (
        f"已从[原会话]({source_url})创建摘要续聊。原会话保留完整历史。\n\n"
        "以下是上一会话的摘要上下文，并非新的用户请求。摘要可能省略细节；"
        "需要核对时请查看原会话。等待用户的新输入后，再继续未完成的任务。\n\n"
        f"{summary}"
    )
    if not consumer_admission_accepts(
        config.budget.consumer_admission, "", [{"role": "assistant", "content": seed}],
    ):
        raise RpcHandlerError("UNAVAILABLE", "续聊摘要超出模型容量，原会话已保留。")
    return seed


async def _create_locked(ctx: Any, session_key: str, decision_id: str) -> dict[str, Any]:
    manager = ctx.session_manager
    storage = get_session_storage(manager)
    assert storage is not None
    await _require_idle(ctx, session_key)
    await _require_accepted(ctx, session_key, decision_id)
    source = await manager.get_session(session_key)
    if source is None:
        raise RpcHandlerError("CONFLICT", "原会话已不存在。")
    target_key, seed_id, binding = _handoff_identity(source, decision_id)
    if await _completed_child(manager, target_key, seed_id, binding) is not None:
        return {"sourceSessionKey": session_key, "targetSessionKey": target_key}
    snapshot = await manager.capture_compaction_source(
        session_key, expected_session_id=source.session_id, expected_session_epoch=source.epoch,
    )
    if not snapshot.valid or not snapshot.entries:
        raise RpcHandlerError("CONFLICT", "会话没有可用于摘要续聊的完整消息。")
    await _require_text_only(ctx, source, snapshot)
    seed = await _summarize(ctx, source, snapshot)
    await _require_idle(ctx, session_key)
    await _require_accepted(ctx, session_key, decision_id)
    current = await manager.capture_compaction_source(
        session_key, expected_session_id=source.session_id, expected_session_epoch=source.epoch,
    )
    if (
        current.preimage != snapshot.preimage
        or current.context_fingerprint != snapshot.context_fingerprint
    ):
        raise RpcHandlerError("CONFLICT", "原会话在摘要期间发生变化，请重新生成摘要。")
    existing = await manager.get_session(target_key)
    if existing is not None:
        metadata = _validate_child(existing, binding)
        if metadata.get("sourceFingerprint") != _source_fingerprint(snapshot):
            raise RpcHandlerError("CONFLICT", "原会话已更新，请重新发起摘要续聊。")
    origin = {
        _ORIGIN_KEY: {
            **binding, "sourceFingerprint": _source_fingerprint(snapshot),
            "sourceBoundaryMessageId": snapshot.boundary_message_id,
            "seedMessageId": seed_id, "seedDigest": _digest(seed),
        },
    }
    if isinstance((source.origin or {}).get(RUN_CONTEXT_ORIGIN_KEY), dict):
        origin[RUN_CONTEXT_ORIGIN_KEY] = deepcopy(source.origin[RUN_CONTEXT_ORIGIN_KEY])

    async def persist() -> None:
        child = await manager.create(
            target_key, agent_id=source.agent_id, reuse_existing=True,
            **{name: deepcopy(getattr(source, name)) for name in _INHERITED_FIELDS},
            parent_session_key=session_key, spawned_by=session_key,
            spawn_depth=source.spawn_depth + 1,
            display_name=f"{source.display_name or source.derived_title or '续聊'} · 摘要续聊",
            origin=origin,
        )
        metadata = _validate_child(child, binding)
        # An interrupted earlier attempt may have created an empty child. Reuse
        # its original summary identity instead of replacing any existing data.
        if metadata.get("seedDigest") != _digest(seed):
            raise RpcHandlerError("CONFLICT", "续聊创建尚未完成，请打开原会话后重试。")
        try:
            await storage.update_session_fields(
                target_key, {"status": SessionStatus.DONE},
                expected_session_id=child.session_id, expected_session_epoch=child.epoch,
            )
            await manager.append_message(
                target_key, "assistant", seed, message_id=seed_id,
                expected_session_id=child.session_id, expected_session_epoch=child.epoch,
                provenance={
                    "kind": "jev_handoff", "origin_session_id": source.session_id,
                    "source_session_key": session_key,
                },
            )
        except BaseException:
            # Never leave an empty destination after a failed seed write, and
            # never remove a destination another caller has already used.
            if not await manager.get_transcript(
                target_key, expected_session_id=child.session_id,
                expected_session_epoch=child.epoch,
            ):
                await storage.delete_session(target_key)
            raise

    target_lock = get_session_lock(ctx.turn_runner, target_key)
    if target_lock is None:
        raise RpcHandlerError("UNAVAILABLE", "暂时无法锁定续聊会话，请稍后重试。")
    async with target_lock:
        await _require_idle(ctx, session_key)
        await _require_idle(ctx, target_key)
        await _require_accepted(ctx, session_key, decision_id)
        await _settle_workspace_operation(persist())
    if await _completed_child(manager, target_key, seed_id, binding) is None:
        raise RpcHandlerError("UNAVAILABLE", "续聊摘要保存失败，原会话已保留。")
    return {"sourceSessionKey": session_key, "targetSessionKey": target_key}


async def create_jev_handoff(ctx: Any, session_key: str, decision_id: str) -> dict[str, Any]:
    """Summarize once, seed a linked child, and leave the source unchanged.

    The caller accepts the JEV decision before entering this function. Neither
    a routing timer nor an in-flight agent iteration can trigger this operation.
    """
    session_key = canonicalize_session_key(session_key)
    if not session_key or not isinstance(decision_id, str) or not decision_id.strip():
        raise RpcHandlerError("INVALID_REQUEST", "缺少原会话或摘要续聊建议。")
    lock = get_session_lock(ctx.turn_runner, session_key)
    if lock is None or get_session_storage(ctx.session_manager) is None:
        raise RpcHandlerError("UNAVAILABLE", "暂时无法锁定原会话，请稍后重试。")
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            await _require_idle(ctx, session_key)
            async with lock:
                return await _create_locked(ctx, session_key, decision_id)
    except StaleEpochError as exc:
        raise RpcHandlerError("CONFLICT", "原会话已重置，请重新发起摘要续聊。") from exc
    except TimeoutError as exc:
        raise RpcHandlerError(
            "UNAVAILABLE", "续聊摘要生成超时，原会话已保留，请稍后重试。",
        ) from exc
