"""Ask JEV about handoff only for a long conversation nearing its model's capacity."""

from __future__ import annotations

import math
import re
from typing import Any

from opensquilla.jev_context import ContextRestartAssessment, ContextRestartDecision


def _nonnegative_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        valid = math.isfinite(value) and value >= 0 and int(value) == value
    except OverflowError:
        return None
    if not valid:
        return None
    return int(value)


def build_context_restart_assessment(
    ctx: Any, *, config: Any, context_window_tokens: int,
) -> ContextRestartAssessment | None:
    """Require both completed conversations and measured context pressure."""
    if (
        not config.enabled or not getattr(config, "session_restart_enabled", True)
        or config.max_prompts_per_hour <= 0
    ):
        return None
    if "jev_completed_conversation_turns" in ctx.metadata:
        completed = _nonnegative_count(ctx.metadata["jev_completed_conversation_turns"])
    else:
        turns = _nonnegative_count(ctx.metadata.get("jev_turn_count", 0))
        completed = max(0, turns - 1) if turns is not None else None
    minimum = int(getattr(config, "context_restart_min_turns", 20))
    if completed is None or completed < minimum:
        return None
    if (
        "routing_history_capacity_estimate_complete" in ctx.metadata
        and ctx.metadata["routing_history_capacity_estimate_complete"] is not True
    ):
        return None
    history_tokens = _nonnegative_count(
        ctx.metadata.get("routing_history_capacity_estimated_tokens", 0),
    )
    if history_tokens is None:
        return None
    absolute = int(getattr(config, "context_restart_min_tokens", 32_000))
    ratio = float(getattr(config, "context_restart_ratio", 0.8))
    window = _nonnegative_count(context_window_tokens)
    if not window:
        return None
    threshold = max(absolute, math.ceil(window * ratio))
    if history_tokens < threshold:
        return None
    return ContextRestartAssessment(
        completed_turns=completed, history_tokens=history_tokens,
        context_window_tokens=window, threshold_tokens=threshold,
    )


async def maybe_publish_context_restart(
    ctx: Any, *, service: Any, config: Any, input_id: str,
    current_model: str, current_provider: str, context_window_tokens: int,
    assessment_stage: str, check_generation: int | None = None,
    assessment: ContextRestartAssessment | None = None,
    decision: ContextRestartDecision | None = None,
) -> bool:
    """Publish a positive native continuity answer without touching execution."""
    if decision is None or decision.action != "new_session":
        return False
    current = build_context_restart_assessment(
        ctx, config=config, context_window_tokens=context_window_tokens,
    )
    if current is None or (assessment is not None and current != assessment):
        return False
    if not math.isfinite(decision.confidence) or not (
        config.confidence_threshold <= decision.confidence <= 1
    ):
        return False
    if check_generation is not None and not service.is_check_current(
        ctx.session_key, check_generation,
    ):
        return False
    # Draft edits share one notice. A declined suggestion can be assessed
    # afresh after a substantial growth in conversation age or history.
    bucket = current.history_tokens // max(1, current.threshold_tokens)
    turn_bucket = current.completed_turns // max(
        2, int(getattr(config, "context_restart_min_turns", 20)),
    )
    chinese = bool(re.search(r"[\u3400-\u9fff]", ctx.semantic_message))
    wire = {
        "action": "new_session",
        "current": {"model": current_model, "provider": current_provider},
        "target": None,
        "reason": (
            "这段会话已完成较多轮对话，上下文也已接近当前模型的容量。JEV 建议生成交接摘要，"
            "带着目标、关键结论和未完成事项在新会话继续；是否搬家由你确认，原会话保留，方便回看。"
            if chinese else
            "This long conversation is approaching its model's context capacity. JEV "
            "recommends a summarized continuation. A handoff "
            "can carry goals, key findings and unfinished work into a new session, if you "
            "confirm. The original conversation remains available."
        ),
        "evidence": [
            f"已完成 {current.completed_turns} 轮对话；"
            f"历史上下文估算 {current.history_tokens:,} / {current.context_window_tokens:,} tokens"
            f"（{current.history_tokens / current.context_window_tokens:.0%}）；"
            f"提醒门槛 {current.threshold_tokens:,} tokens"
            if chinese else
            f"{current.completed_turns} completed conversational turns; "
            f"estimated history: {current.history_tokens:,} / {current.context_window_tokens:,} "
            f"tokens ({current.history_tokens / current.context_window_tokens:.0%}); "
            f"required threshold: {current.threshold_tokens:,} tokens."
        ],
        "risk": "medium",
        "trigger": "context_pressure",
        "source": "loop" if assessment_stage == "loop" else "input",
        "handoff": {
            "sourceSessionKey": ctx.session_key,
            "contextTokens": current.history_tokens,
            "contextWindowTokens": current.context_window_tokens,
            "completedTurns": current.completed_turns,
        },
    }
    record = await service.publish(
        ctx.session_key, wire,
        input_id=f"context-restart:jev:{current.context_window_tokens}:{bucket}:{turn_bucket}",
        max_prompts_per_hour=config.max_prompts_per_hour,
        check_generation=check_generation,
    )
    if not record or record.get("status") != "pending":
        return False
    ctx.metadata["jev_context_notice_id"] = record["id"]
    await service.notify(ctx.session_key)
    return True
