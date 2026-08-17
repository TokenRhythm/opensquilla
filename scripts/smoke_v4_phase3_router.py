#!/usr/bin/env python3
"""Run real V4 Phase 3 router smoke scenarios and a OpenSquilla gateway startup check."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from opensquilla.engine.pipeline import TurnContext  # noqa: E402
from opensquilla.engine.steps import squilla_router as router_mod  # noqa: E402
from opensquilla.env import load_env  # noqa: E402
from opensquilla.gateway.config import GatewayConfig  # noqa: E402

TIERS = {
    "c0": {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash",
        "description": "short text and trivial follow-ups",
        "thinking_level": "high",
    },
    "c1": {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-pro",
        "description": "normal coding and agent tasks",
        "thinking_level": "high",
    },
    "c2": {
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "description": "structured multi-step work",
        "thinking_level": "high",
    },
    "c3": {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "description": "deep reasoning and hard recovery turns",
        "thinking_level": "high",
    },
}
TIER_ORDER = list(TIERS)
ROUTE_CLASS_EXAMPLES = [
    {
        "id": "r0_thanks",
        "expected_route_class": "R0",
        "description": "Short acknowledgement that should stay on the cheapest tier.",
        "message": "谢谢。",
    },
    {
        "id": "r1_database_comparison",
        "expected_route_class": "R1",
        "description": "Moderate structured comparison with bounded reasoning.",
        "message": "比较 PostgreSQL 和 MySQL 在事务、索引、复制方面的差异，用表格输出。",
    },
    {
        "id": "r2_async_timeout_debug",
        "expected_route_class": "R2",
        "description": "Multi-signal debugging task that should use a reasoning tier.",
        "message": (
            "下面是一个异步服务偶发超时的日志片段，请定位可能原因并给出排查步骤："
            "连接池耗尽、慢查询、重试风暴、队列积压同时出现。"
        ),
    },
    {
        "id": "r3_distributed_scheduler",
        "expected_route_class": "R3",
        "description": "Deep architecture task with consistency and failure-recovery tradeoffs.",
        "message": "请设计一个跨机房分布式任务调度系统，要求解释一致性、故障恢复和容量评估。",
    },
]
SINGLE_TURN_ROUTER_CASES = [
    {
        "id": "single_r0_thanks",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "谢谢。",
    },
    {
        "id": "single_r0_ack",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "好的。",
    },
    {
        "id": "single_r0_continue",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "继续。",
    },
    {
        "id": "single_r0_yes",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "是的。",
    },
    {
        "id": "single_r0_no",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "不用了。",
    },
    {
        "id": "single_r0_short_translate",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "把 hello 翻译成中文。",
    },
    {
        "id": "single_r0_one_line",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "用一句话解释 API。",
    },
    {
        "id": "single_r0_confirm",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "收到。",
    },
    {
        "id": "single_r0_brief_reply",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "给我一个短标题。",
    },
    {
        "id": "single_r1_db_compare",
        "category": "single_turn_r1",
        "expected_route_class": "R1",
        "message": "比较 PostgreSQL 和 MySQL 在事务、索引、复制方面的差异，用表格输出。",
    },
    {
        "id": "single_r1_git_rebase",
        "category": "single_turn_r1",
        "expected_route_class": "R1",
        "message": "解释 git rebase 和 git merge 的区别，并给出适用场景。",
    },
    {
        "id": "single_r1_api_errors",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "列出 REST API 常见错误码，并说明前端应该如何展示。",
    },
    {
        "id": "single_r1_release_note",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "根据新增登录限流功能写一段简短 release note。",
    },
    {
        "id": "single_r1_sql_query",
        "category": "single_turn_r1",
        "expected_route_class": "R1",
        "message": "写一个 SQL 查询，统计每天新增用户数并按日期排序。",
    },
    {
        "id": "single_r1_docker_steps",
        "category": "single_turn_r1",
        "expected_route_class": "R1",
        "message": "给出 Docker 容器内查看环境变量和日志的排查步骤。",
    },
    {
        "id": "single_r1_email",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "帮我写一封礼貌的延期交付说明邮件。",
    },
    {
        "id": "single_r1_regex",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "写一个正则表达式匹配 yyyy-mm-dd 日期，并解释每一段。",
    },
    {
        "id": "single_r1_markdown_table",
        "category": "single_turn_r1",
        "expected_route_class": "R1",
        "message": "把缓存、队列、数据库三个组件的职责整理成 Markdown 表格。",
    },
    {
        "id": "single_r2_async_timeout_debug",
        "category": "single_turn_r2",
        "expected_route_class": "R2",
        "message": (
            "下面是一个异步服务偶发超时的日志片段，请定位可能原因并给出排查步骤："
            "连接池耗尽、慢查询、重试风暴、队列积压同时出现。"
        ),
    },
    {
        "id": "single_r2_flaky_test",
        "category": "single_turn_r2",
        "expected_route_class": "R2",
        "message": (
            "一个 pytest 用例偶发失败，涉及时间冻结、异步任务和共享缓存，"
            "请给出系统排查方案。"
        ),
    },
    {
        "id": "single_r2_sql_optimization",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "分析一个慢 SQL：多表 join、where 条件低选择性、排序分页很慢，给出优化顺序。",
    },
    {
        "id": "single_r2_memory_leak",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "Python 服务运行 6 小时后内存翻倍，请设计定位内存泄漏的步骤和观测指标。",
    },
    {
        "id": "single_r2_k8s_pending",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "Kubernetes Pod 一直 Pending，节点资源、亲和性、PVC 都可能相关，请给出排查树。",
    },
    {
        "id": "single_r2_migration_plan",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "设计一个从同步任务迁移到异步队列的分阶段方案，要求可回滚且不中断用户请求。",
    },
    {
        "id": "single_r2_security_bug",
        "category": "single_turn_r0",
        "expected_route_class": "R0",
        "message": "审查一个上传接口的安全风险，涉及文件类型伪造、路径穿越和大文件 DoS。",
    },
    {
        "id": "single_r2_perf_regression",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "发布后 P95 延迟从 300ms 涨到 2s，请给出从指标到代码的回归定位流程。",
    },
    {
        "id": "single_r2_state_machine",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "为订单支付状态机设计异常流转处理，包含超时、重复回调和人工退款。",
    },
    {
        "id": "single_r3_distributed_scheduler",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "请设计一个跨机房分布式任务调度系统，要求解释一致性、故障恢复和容量评估。",
    },
    {
        "id": "single_r3_model_router_arch",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "设计一个成本感知模型路由器，包含特征、训练、在线推理、灰度、回滚和观测体系。",
    },
    {
        "id": "single_r3_event_sourcing",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "为金融账务系统设计 event sourcing 架构，要求说明一致性、审计、补偿和灾备。",
    },
    {
        "id": "single_r3_multi_agent_platform",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "设计一个多 Agent 协作平台，要求任务拆分、权限边界、失败恢复和成本控制。",
    },
    {
        "id": "single_r3_global_cache",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "设计全球多区域缓存系统，要求处理热点、失效、一致性、容量规划和故障演练。",
    },
    {
        "id": "single_r3_zero_downtime_migration",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "规划一个核心数据库零停机分库分表迁移，包含双写、校验、切流和回滚策略。",
    },
    {
        "id": "single_r3_privacy_compliance",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "设计一个跨境数据处理平台，要求兼顾隐私合规、审计追踪、数据最小化和可删除性。",
    },
    {
        "id": "single_r3_consensus_recovery",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "解释 Raft 集群在网络分区、leader 抖动和日志冲突下的恢复流程，并给出工程防护。",
    },
    {
        "id": "single_r3_incident_command",
        "category": "single_turn_r3",
        "expected_route_class": "R3",
        "message": "制定一次大规模支付故障的 incident command 方案，包含止血、沟通、恢复和复盘。",
    },
]
DIALOGUE_ROUTER_CASES = [
    {
        "id": "dialogue_r3_then_summarize",
        "category": "continuous_kv_cache",
        "description": (
            "A hard architecture turn followed by a short compression request should keep c3."
        ),
        "turns": [
            {
                "message": (
                    "请设计一个跨机房分布式任务调度系统，"
                    "要求解释一致性、故障恢复和容量评估。"
                ),
                "expected_route_class": "R3",
            },
            {
                "message": "好的，把上面的方案压缩成三句话。",
                "expected_route_class": "R1",
                "expect_anti_downgrade": True,
                "expected_final_tier": "c3",
            },
        ],
    },
    {
        "id": "dialogue_r3_then_ack",
        "category": "continuous_kv_cache",
        "description": (
            "A trivial acknowledgement inside the 10-minute window should not downgrade."
        ),
        "turns": [
            {
                "message": (
                    "设计全球多区域缓存系统，"
                    "要求处理热点、失效、一致性、容量规划和故障演练。"
                ),
                "expected_route_class": "R3",
            },
            {
                "message": "收到。",
                "expected_route_class": "R0",
                "expect_anti_downgrade": True,
                "expected_final_tier": "c3",
            },
        ],
    },
    {
        "id": "dialogue_r2_then_thanks",
        "category": "continuous_kv_cache",
        "description": (
            "A debugging flow should preserve the previous reasoning tier for a short follow-up."
        ),
        "turns": [
            {
                "message": "Python 服务运行 6 小时后内存翻倍，请设计定位内存泄漏的步骤和观测指标。",
                "expected_route_class": "R3",
            },
            {
                "message": "谢谢，先按这个方向查。",
                "expected_route_class": "R1",
                "expect_anti_downgrade": True,
                "expected_final_tier": "c3",
            },
        ],
    },
    {
        "id": "dialogue_r2_then_more_detail",
        "category": "continuous_kv_cache",
        "description": "A reasoning task followed by detail expansion should stay at or above c2.",
        "turns": [
            {
                "message": "发布后 P95 延迟从 300ms 涨到 2s，请给出从指标到代码的回归定位流程。",
                "expected_route_class": "R3",
            },
            {
                "message": "继续展开数据库和缓存两个方向。",
                "expected_route_class": "R2",
                "expect_anti_downgrade": True,
                "expected_final_tier": "c3",
            },
        ],
    },
    {
        "id": "dialogue_r1_then_example",
        "category": "continuous_followup",
        "description": "A normal task and a normal follow-up should remain in the standard lane.",
        "turns": [
            {
                "message": "解释 git rebase 和 git merge 的区别，并给出适用场景。",
                "expected_route_class": "R1",
            },
            {
                "message": "再给一个团队协作中的例子。",
                "expected_route_class": "R1",
                "expected_final_tier": "c1",
            },
        ],
    },
    {
        "id": "dialogue_r1_then_ack",
        "category": "continuous_followup",
        "description": (
            "A standard task followed by acknowledgement should keep the active c1 model."
        ),
        "turns": [
            {
                "message": "比较 PostgreSQL 和 MySQL 在事务、索引、复制方面的差异，用表格输出。",
                "expected_route_class": "R1",
            },
            {
                "message": "好的。",
                "expected_route_class": "R0",
                "expect_anti_downgrade": True,
                "expected_final_tier": "c1",
            },
        ],
    },
    {
        "id": "dialogue_complaint_after_r0",
        "category": "complaint_upgrade",
        "description": "A complaint on a trivial request should upgrade one tier.",
        "turns": [
            {
                "message": "谢谢。",
                "expected_route_class": "R0",
            },
            {
                "message": "不对，你没理解我的意思，重新回答。",
                "expected_route_class": "R0",
                "expect_complaint_upgrade": True,
                "expected_final_tier": "c1",
            },
        ],
    },
    {
        "id": "dialogue_complaint_after_r1",
        "category": "complaint_upgrade",
        "description": "A complaint after a standard task should move the current turn upward.",
        "turns": [
            {
                "message": "比较 PostgreSQL 和 MySQL 在事务、索引、复制方面的差异，用表格输出。",
                "expected_route_class": "R1",
            },
            {
                "message": "这不对，太泛了，按客户视角重新写。",
                "expected_route_class": "R2",
                "expect_complaint_upgrade": True,
                "expected_final_tier": "c3",
            },
        ],
    },
    {
        "id": "dialogue_complaint_after_r2",
        "category": "complaint_upgrade",
        "description": (
            "A complaint inside an active reasoning flow should preserve at least the "
            "reasoning tier."
        ),
        "turns": [
            {
                "message": (
                    "下面是一个异步服务偶发超时的日志片段，请定位可能原因并给出排查步骤："
                    "连接池耗尽、慢查询、重试风暴、队列积压同时出现。"
                ),
                "expected_route_class": "R2",
            },
            {
                "message": "不对，刚才漏掉了 scheduler event 和 admission webhook，重新排查。",
                "expected_route_class": "R1",
                "expect_complaint_upgrade": True,
                "expected_final_tier": "c3",
            },
        ],
    },
    {
        "id": "dialogue_three_turn_r3_sticky",
        "category": "continuous_kv_cache",
        "description": (
            "Multiple short follow-ups after an R3 turn should preserve the active "
            "frontier tier."
        ),
        "turns": [
            {
                "message": (
                    "设计一个多 Agent 协作平台，"
                    "要求任务拆分、权限边界、失败恢复和成本控制。"
                ),
                "expected_route_class": "R3",
            },
            {
                "message": "先只保留权限边界。",
                "expected_route_class": "R0",
                "expect_anti_downgrade": True,
                "expected_final_tier": "c3",
            },
            {
                "message": "再压缩成三条。",
                "expected_route_class": "R0",
                "expect_anti_downgrade": True,
                "expected_final_tier": "c3",
            },
        ],
    },
    {
        "id": "dialogue_three_turn_r2_sticky",
        "category": "continuous_kv_cache",
        "description": "A multi-turn debugging flow should keep c2 across brief narrowing turns.",
        "turns": [
            {
                "message": (
                    "一个 pytest 用例偶发失败，涉及时间冻结、异步任务和共享缓存，"
                    "请给出系统排查方案。"
                ),
                "expected_route_class": "R2",
            },
            {
                "message": "只看异步任务这一块。",
                "expected_route_class": "R0",
                "expect_anti_downgrade": True,
                "expected_final_tier": "c2",
            },
            {
                "message": "再压缩成三条。",
                "expected_route_class": "R0",
                "expect_anti_downgrade": True,
                "expected_final_tier": "c2",
            },
        ],
    },
    {
        "id": "dialogue_r0_to_r1_no_sticky_jump",
        "category": "continuous_followup",
        "description": (
            "A trivial first turn should not block a later standard task from choosing c1."
        ),
        "turns": [
            {
                "message": "收到。",
                "expected_route_class": "R0",
            },
            {
                "message": "比较 PostgreSQL 和 MySQL 在事务、索引、复制方面的差异，用表格输出。",
                "expected_route_class": "R1",
                "expected_final_tier": "c1",
            },
        ],
    },
    {
        "id": "dialogue_r0_to_r2_upgrade",
        "category": "continuous_followup",
        "description": (
            "A trivial first turn should allow a later debugging request to upgrade normally."
        ),
        "turns": [
            {
                "message": "好的。",
                "expected_route_class": "R0",
            },
            {
                "message": (
                    "下面是一个异步服务偶发超时的日志片段，请定位可能原因并给出排查步骤："
                    "连接池耗尽、慢查询、重试风暴、队列积压同时出现。"
                ),
                "expected_route_class": "R2",
                "expected_final_tier": "c2",
            },
        ],
    },
    {
        "id": "dialogue_r1_to_r3_upgrade",
        "category": "continuous_followup",
        "description": (
            "A standard first turn should allow a later architecture request to upgrade to c3."
        ),
        "turns": [
            {
                "message": "把缓存、队列、数据库三个组件的职责整理成 Markdown 表格。",
                "expected_route_class": "R1",
            },
            {
                "message": "现在基于这三个组件设计一个跨区域高可用架构，说明故障恢复和容量评估。",
                "expected_route_class": "R3",
                "expected_final_tier": "c3",
            },
        ],
    },
]


def _tier_rank(tier: str | None) -> int:
    return TIER_ORDER.index(tier) if tier in TIER_ORDER else -1


def _reset_router_state(*, reset_strategy: bool = False) -> None:
    router_mod._history_store.clear()
    if reset_strategy:
        router_mod._strategy = None
        router_mod._strategy_key = None


def _router_config() -> GatewayConfig:
    config = GatewayConfig()
    config.squilla_router = config.squilla_router.model_copy(
        update={
            "enabled": True,
            "strategy": "v4_phase3",
            "rollout_phase": "full",
            "tiers": TIERS,
            "default_tier": "c1",
            "require_router_runtime": True,
            "confidence_threshold": 0.5,
            "kv_cache_anti_downgrade_enabled": True,
            "kv_cache_anti_downgrade_window_seconds": 600,
            "complaint_upgrade_enabled": True,
            "complaint_upgrade_steps": 1,
        }
    )
    return config


async def _route_turn(message: str, session_key: str, metadata: dict | None = None) -> TurnContext:
    ctx = TurnContext(
        message=message,
        raw_message=message,
        session_key=session_key,
        config=_router_config(),
        provider=None,
        model="smoke-default-model",
        tool_defs=[],
        system_prompt="",
        attachments=[],
        metadata=metadata or {},
    )
    return await router_mod.apply_squilla_router(ctx)


def _scenario_from_ctx(name: str, ctx: TurnContext, **fields: Any) -> dict[str, Any]:
    extra = ctx.metadata.get("routing_extra") or {}
    return {
        "name": name,
        "ok": True,
        "base_tier": extra.get("base_tier"),
        "final_tier": ctx.metadata.get("routed_tier"),
        "route_class": extra.get("route_class"),
        "final_route_class": extra.get("final_route_class"),
        "model": ctx.metadata.get("routed_model"),
        "confidence": ctx.metadata.get("routing_confidence"),
        "thinking_mode": ctx.metadata.get("thinking_mode"),
        "thinking_level": ctx.metadata.get("thinking_level"),
        "prompt_policy": ctx.metadata.get("prompt_policy"),
        "prompt_hint_injected": "[RESPONSE_POLICY:" in ctx.message,
        **fields,
    }


async def _continuous_dialogue_scenario() -> dict[str, Any]:
    _reset_router_state()
    session = "smoke-continuous-dialogue"
    first = await _route_turn(
        "请设计一个跨机房分布式任务调度系统，要求解释一致性、故障恢复和容量评估。",
        session,
    )
    second = await _route_turn("好的，把上面的方案压缩成三句话。", session)
    history = second.metadata.get("routing_history") or []
    first_extra = first.metadata.get("routing_extra") or {}
    second_extra = second.metadata.get("routing_extra") or {}
    first_tier = first.metadata.get("routed_tier")
    second_tier = second.metadata.get("routed_tier")
    ok = _tier_rank(second_tier) >= _tier_rank(first_tier) and len(history) >= 2
    scenario = _scenario_from_ctx(
        "continuous_dialogue_kv_cache_reuse",
        second,
        ok=ok,
        first_base_tier=first_extra.get("base_tier"),
        first_final_tier=first_tier,
        second_base_tier=second_extra.get("base_tier"),
        second_final_tier=second_tier,
        anti_downgrade_applied=second_extra.get("anti_downgrade_applied"),
        previous_tier=second_extra.get("previous_tier"),
        routing_history=history,
    )
    scenario["ok"] = ok
    return scenario


async def _forced_recent_history_scenario() -> dict[str, Any]:
    _reset_router_state()
    session = "smoke-forced-recent-history"
    router_mod._history_store.set(
        session,
        [
            {
                "turn_index": 0,
                "_ts": time.monotonic(),
                "text": "上一轮是一个长上下文的架构设计问题。",
                "route_class": "R3",
                "final_route_class": "R3",
                "base_tier": "c3",
                "final_tier": "c3",
                "difficulty": 2.0,
                "margin": 0.8,
                "top1_label": "R3",
            }
        ],
    )
    ctx = await _route_turn("谢谢。", session)
    extra = ctx.metadata.get("routing_extra") or {}
    ok = (
        extra.get("anti_downgrade_applied") is True
        and ctx.metadata.get("routed_tier") == "c3"
        and extra.get("previous_tier") == "c3"
    )
    scenario = _scenario_from_ctx(
        "forced_recent_history_blocks_downgrade",
        ctx,
        ok=ok,
        anti_downgrade_applied=extra.get("anti_downgrade_applied"),
        previous_tier=extra.get("previous_tier"),
        routing_history=ctx.metadata.get("routing_history") or [],
    )
    scenario["ok"] = ok
    return scenario


async def _complaint_upgrade_scenario() -> dict[str, Any]:
    _reset_router_state()
    ctx = await _route_turn("不对，你没理解我的意思，重新回答。", "smoke-complaint-upgrade")
    extra = ctx.metadata.get("routing_extra") or {}
    base_tier = extra.get("base_tier")
    final_tier = ctx.metadata.get("routed_tier")
    ok = (
        extra.get("complaint_detected") is True
        and extra.get("complaint_upgrade_applied") is True
        and _tier_rank(final_tier) >= _tier_rank(base_tier) + 1
    )
    scenario = _scenario_from_ctx(
        "complaint_upgrades_current_turn",
        ctx,
        ok=ok,
        complaint_detected=extra.get("complaint_detected"),
        complaint_upgrade_applied=extra.get("complaint_upgrade_applied"),
        complaint_terms=extra.get("complaint_terms"),
    )
    scenario["ok"] = ok
    return scenario


async def _route_class_coverage_scenario() -> dict[str, Any]:
    examples: list[dict[str, Any]] = []
    for item in ROUTE_CLASS_EXAMPLES:
        _reset_router_state()
        ctx = await _route_turn(
            str(item["message"]),
            f"smoke-route-coverage-{item['id']}",
        )
        extra = ctx.metadata.get("routing_extra") or {}
        actual_route_class = extra.get("route_class")
        examples.append(
            {
                "id": item["id"],
                "description": item["description"],
                "message": item["message"],
                "expected_route_class": item["expected_route_class"],
                "actual_route_class": actual_route_class,
                "base_tier": extra.get("base_tier"),
                "final_tier": ctx.metadata.get("routed_tier"),
                "confidence": ctx.metadata.get("routing_confidence"),
                "difficulty": extra.get("difficulty"),
                "margin": extra.get("margin"),
                "thinking_mode": ctx.metadata.get("thinking_mode"),
                "prompt_policy": ctx.metadata.get("prompt_policy"),
                "probabilities": extra.get("probabilities"),
                "ok": actual_route_class == item["expected_route_class"],
            }
        )

    covered = sorted({str(example["actual_route_class"]) for example in examples})
    ok = all(example["ok"] for example in examples) and covered == ["R0", "R1", "R2", "R3"]
    return {
        "name": "route_class_coverage_r0_to_r3",
        "ok": ok,
        "covered_route_classes": covered,
        "examples": examples,
    }


def _case_turn_result(ctx: TurnContext, expected_route_class: str | None = None) -> dict[str, Any]:
    extra = ctx.metadata.get("routing_extra") or {}
    actual_route_class = extra.get("route_class")
    final_tier = ctx.metadata.get("routed_tier")
    result = {
        "message": ctx.raw_message,
        "expected_route_class": expected_route_class,
        "actual_route_class": actual_route_class,
        "base_tier": extra.get("base_tier"),
        "final_tier": final_tier,
        "final_route_class": extra.get("final_route_class"),
        "confidence": ctx.metadata.get("routing_confidence"),
        "difficulty": extra.get("difficulty"),
        "margin": extra.get("margin"),
        "thinking_mode": ctx.metadata.get("thinking_mode"),
        "thinking_level": ctx.metadata.get("thinking_level"),
        "prompt_policy": ctx.metadata.get("prompt_policy"),
        "anti_downgrade_applied": extra.get("anti_downgrade_applied"),
        "complaint_detected": extra.get("complaint_detected"),
        "complaint_upgrade_applied": extra.get("complaint_upgrade_applied"),
        "previous_tier": extra.get("previous_tier"),
        "probabilities": extra.get("probabilities"),
    }
    result["ok"] = expected_route_class is None or actual_route_class == expected_route_class
    return result


def _turn_expectations_ok(result: dict[str, Any], turn: dict[str, Any]) -> bool:
    ok = bool(result.get("ok"))
    if "expected_final_tier" in turn:
        ok = ok and result.get("final_tier") == turn["expected_final_tier"]
    if turn.get("expect_anti_downgrade"):
        ok = ok and result.get("anti_downgrade_applied") is True
    if turn.get("expect_complaint_upgrade"):
        ok = (
            ok
            and result.get("complaint_detected") is True
            and result.get("complaint_upgrade_applied") is True
        )
    return ok


async def _single_turn_case_result(case: dict[str, Any]) -> dict[str, Any]:
    _reset_router_state()
    ctx = await _route_turn(
        str(case["message"]),
        f"smoke-case-{case['id']}",
    )
    turn = _case_turn_result(ctx, str(case["expected_route_class"]))
    ok = _turn_expectations_ok(turn, case)
    return {
        "id": case["id"],
        "kind": "single_turn",
        "category": case["category"],
        "ok": ok,
        **turn,
    }


async def _dialogue_case_result(case: dict[str, Any]) -> dict[str, Any]:
    _reset_router_state()
    session = f"smoke-case-{case['id']}"
    turns: list[dict[str, Any]] = []
    for index, turn_spec in enumerate(case["turns"], start=1):
        ctx = await _route_turn(str(turn_spec["message"]), session)
        turn = _case_turn_result(ctx, str(turn_spec["expected_route_class"]))
        turn["index"] = index
        turn["ok"] = _turn_expectations_ok(turn, turn_spec)
        turns.append(turn)

    last_turn = turns[-1]
    return {
        "id": case["id"],
        "kind": "dialogue",
        "category": case["category"],
        "description": case["description"],
        "ok": all(turn["ok"] for turn in turns),
        "turn_count": len(turns),
        "actual_route_class": last_turn.get("actual_route_class"),
        "final_tier": last_turn.get("final_tier"),
        "turns": turns,
    }


async def _route_case_suite_scenario() -> dict[str, Any]:
    _reset_router_state(reset_strategy=True)
    cases: list[dict[str, Any]] = []
    for case in SINGLE_TURN_ROUTER_CASES:
        cases.append(await _single_turn_case_result(case))
    for case in DIALOGUE_ROUTER_CASES:
        cases.append(await _dialogue_case_result(case))

    covered = sorted(
        {
            str(turn["actual_route_class"])
            for case in cases
            for turn in (case.get("turns") or [case])
            if turn.get("actual_route_class")
        }
    )
    case_counts_by_route = Counter(str(case.get("actual_route_class")) for case in cases)
    ok = (
        len(cases) == 50
        and len(SINGLE_TURN_ROUTER_CASES) >= 32
        and len(DIALOGUE_ROUTER_CASES) >= 12
        and covered == ["R0", "R1", "R2", "R3"]
        and all(case_counts_by_route[route] >= 4 for route in ("R0", "R1", "R2", "R3"))
        and all(case["ok"] for case in cases)
    )
    return {
        "name": "route_case_suite_50",
        "ok": ok,
        "case_count": len(cases),
        "single_turn_count": len(SINGLE_TURN_ROUTER_CASES),
        "dialogue_count": len(DIALOGUE_ROUTER_CASES),
        "covered_route_classes": covered,
        "case_counts_by_route": dict(case_counts_by_route),
        "cases": cases,
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_json(url: str, timeout: float = 1.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _live_tier_model_map(live_model: str) -> dict[str, str]:
    def _tier_model_env(canonical: str, legacy: str, default: str) -> str:
        return os.environ.get(canonical, os.environ.get(legacy, default)).strip()

    live_tier_models = {
        "c0": _tier_model_env(
            "OPENSQUILLA_LIVE_LLM_C0_MODEL",
            "OPENSQUILLA_LIVE_LLM_T0_MODEL",
            "deepseek/deepseek-v4-flash",
        ),
        "c1": _tier_model_env(
            "OPENSQUILLA_LIVE_LLM_C1_MODEL",
            "OPENSQUILLA_LIVE_LLM_T1_MODEL",
            "deepseek/deepseek-v4-pro",
        ),
        "c2": _tier_model_env(
            "OPENSQUILLA_LIVE_LLM_C2_MODEL",
            "OPENSQUILLA_LIVE_LLM_T2_MODEL",
            "z-ai/glm-5.2",
        ),
        "c3": _tier_model_env(
            "OPENSQUILLA_LIVE_LLM_C3_MODEL",
            "OPENSQUILLA_LIVE_LLM_T3_MODEL",
            "anthropic/claude-opus-4.8",
        ),
    }
    if live_model:
        return {tier: live_model for tier in live_tier_models}
    return live_tier_models


def _write_live_gateway_config(path: Path, live_model: str) -> None:
    live_tier_models = _live_tier_model_map(live_model)
    tier_blocks = []
    for tier, description in {
        "c0": "live smoke fast route",
        "c1": "live smoke standard route",
        "c2": "live smoke reasoning route",
        "c3": "live smoke frontier route",
    }.items():
        tier_blocks.append(
            f"""
[squilla_router.tiers.{tier}]
provider = "openrouter"
model = "{live_tier_models[tier]}"
description = "{description}"
supports_image = false
"""
        )
    path.write_text(
        f"""
host = "127.0.0.1"
debug = false
llm_request_timeout_seconds = 90

[auth]
mode = "none"

[control_ui]
enabled = false

[rate_limit]
enabled = false

[memory]
source = "state"

[llm]
provider = "openrouter"
model = "{live_model or live_tier_models['c1']}"
base_url = "https://openrouter.ai/api/v1"
max_tokens = 192

[squilla_router]
enabled = true
auto_thinking = true
rollout_phase = "full"
strategy = "v4_phase3"
default_tier = "c1"
confidence_threshold = 0.5
kv_cache_anti_downgrade_enabled = true
kv_cache_anti_downgrade_window_seconds = 600
complaint_upgrade_enabled = true
complaint_upgrade_steps = 1
require_router_runtime = true
{''.join(tier_blocks)}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _assistant_messages(history: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in history.get("messages", []) if m.get("role") == "assistant"]


def _system_errors(history: dict[str, Any]) -> list[str]:
    return [
        str(m.get("text", ""))
        for m in history.get("messages", [])
        if m.get("role") == "system" and str(m.get("text", "")).startswith("Error:")
    ]


def _wait_for_assistant_reply(
    *,
    port: int,
    session_key: str,
    previous_assistant_count: int,
    timeout_seconds: float = 120.0,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    quoted = urllib.parse.quote(session_key, safe="")
    deadline = time.monotonic() + timeout_seconds
    last_history: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            history = _read_json(
                f"http://127.0.0.1:{port}/api/chat/history?sessionKey={quoted}",
                timeout=3.0,
            )
            last_history = history
            errors = _system_errors(history)
            if errors:
                return None, history, errors[-1]
            assistants = _assistant_messages(history)
            if len(assistants) > previous_assistant_count:
                latest = assistants[-1]
                if str(latest.get("text", "")).strip():
                    return latest, history, None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    return None, last_history, "assistant reply did not arrive before timeout"


def _read_turn_call_records(log_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("turn-calls-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _summarize_llm_request_context(
    records: list[dict[str, Any]],
    *,
    session_keys: set[str] | None = None,
) -> dict[str, Any]:
    requests = [
        record
        for record in records
        if record.get("kind") == "llm_request"
        and (session_keys is None or record.get("session_key") in session_keys)
    ]
    samples: list[dict[str, Any]] = []
    with_prev_user = 0
    with_prev_assistant = 0
    with_history = 0
    models: set[str] = set()
    for record in requests:
        model = str(record.get("model") or "")
        if model:
            models.add(model)
        messages = (record.get("payload") or {}).get("messages") or []
        roles = [message.get("role") for message in messages if isinstance(message, dict)]
        previous_roles = roles[:-1]
        has_prev_user = "user" in previous_roles
        has_prev_assistant = "assistant" in previous_roles
        if len(roles) > 1:
            with_history += 1
        if has_prev_user:
            with_prev_user += 1
        if has_prev_assistant:
            with_prev_assistant += 1
        if (has_prev_user or has_prev_assistant) and len(samples) < 8:
            samples.append(
                {
                    "session_key": record.get("session_key"),
                    "model": model,
                    "message_count": len(roles),
                    "roles_tail": roles[-6:],
                    "has_prev_user": has_prev_user,
                    "has_prev_assistant": has_prev_assistant,
                }
            )
    return {
        "llm_request_count": len(requests),
        "requests_with_history": with_history,
        "requests_with_prev_user": with_prev_user,
        "requests_with_prev_assistant": with_prev_assistant,
        "models": sorted(models),
        "samples": samples,
    }


def _usage_from_llm_responses(records: list[dict[str, Any]]) -> dict[str, Any]:
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    cached_tokens = 0
    total_cost = 0.0
    models: set[str] = set()
    for record in records:
        usage = ((record.get("payload") or {}).get("usage") or {})
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
        reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
        cached_tokens += int(usage.get("cached_tokens") or 0)
        total_cost += float(usage.get("billed_cost") or 0.0)
        model = usage.get("model")
        if model:
            models.add(str(model))
    return {
        "totalInputTokens": input_tokens,
        "totalOutputTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens + reasoning_tokens,
        "reasoningTokens": reasoning_tokens,
        "cachedTokens": cached_tokens,
        "totalCostUsd": total_cost,
        "models": sorted(models),
        "source": "turn_call_log",
    }


def _wait_for_gateway_health(
    proc: subprocess.Popen,
    port: int,
) -> tuple[dict[str, Any] | None, str | None]:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=1)
            return None, f"gateway exited early with code {proc.returncode}: {stderr or stdout}"
        try:
            return _read_json(f"http://127.0.0.1:{port}/health"), None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(0.25)
    return None, "gateway did not become healthy before timeout"


def _stop_gateway(proc: subprocess.Popen) -> tuple[str, str]:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    try:
        stdout_tail = (proc.stdout.read() if proc.stdout else "")[-2000:]
    except ValueError:
        stdout_tail = ""
    try:
        stderr_tail = (proc.stderr.read() if proc.stderr else "")[-2000:]
    except ValueError:
        stderr_tail = ""
    return stdout_tail, stderr_tail


def _gateway_startup_scenario() -> dict[str, Any]:
    port = _free_port()
    with tempfile.TemporaryDirectory(
        prefix="opensquilla-router-smoke-",
        ignore_cleanup_errors=True,
    ) as tmp:
        tmp_path = Path(tmp)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = str(tmp_path / "no-config.toml")
        env["OPENSQUILLA_STATE_DIR"] = str(tmp_path / "state")
        env["OPENSQUILLA_AUTH_MODE"] = "none"
        env["OPENSQUILLA_CONTROL_UI_ENABLED"] = "false"
        env["OPENSQUILLA_RATE_ENABLED"] = "false"
        env["OPENSQUILLA_MEMORY_SOURCE"] = "state"
        env["OPENSQUILLA_MEMORY_DREAM_DISABLED"] = "1"
        env["OPENSQUILLA_SANDBOX_SANDBOX"] = "false"
        env["OPENSQUILLA_SANDBOX_SECURITY_GRADING"] = "false"
        env["OPENSQUILLA_SQUILLA_ROUTER_ENABLED"] = "true"
        env["OPENSQUILLA_SQUILLA_ROUTER_STRATEGY"] = "v4_phase3"
        env["OPENSQUILLA_SQUILLA_ROUTER_ROLLOUT_PHASE"] = "full"
        env["OPENSQUILLA_SQUILLA_ROUTER_REQUIRE_ROUTER_RUNTIME"] = "true"
        env["OPENROUTER_API_KEY"] = ""

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "opensquilla.cli.main",
                "gateway",
                "run",
                "--port",
                str(port),
                "--bind",
                "127.0.0.1",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        health: dict[str, Any] | None = None
        public_config: dict[str, Any] | None = None
        error: str | None = None
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=1)
                    error = f"gateway exited early with code {proc.returncode}: {stderr or stdout}"
                    break
                try:
                    health = _read_json(f"http://127.0.0.1:{port}/health")
                    public_config = _read_json(f"http://127.0.0.1:{port}/api/config")
                    break
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                    time.sleep(0.25)
            if health is None and error is None:
                error = "gateway did not become healthy before timeout"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            stdout_tail = (proc.stdout.read() if proc.stdout else "")[-2000:]
            stderr_tail = (proc.stderr.read() if proc.stderr else "")[-2000:]

    squilla_router_config = (public_config or {}).get("squilla_router") or {}
    ok = (
        error is None
        and health is not None
        and health.get("ok") is True
        and squilla_router_config.get("strategy") == "v4_phase3"
        and squilla_router_config.get("enabled") is True
    )
    return {
        "name": "opensquilla_gateway_startup",
        "ok": ok,
        "port": port,
        "health": health or {},
        "config": {"squilla_router": squilla_router_config},
        "error": error,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }


def _live_llm_chat_scenario() -> dict[str, Any]:
    api_key_set = bool(os.environ.get("OPENROUTER_API_KEY"))
    if not api_key_set:
        return {
            "name": "opensquilla_gateway_live_llm_chat",
            "ok": False,
            "error": "OPENROUTER_API_KEY is required for --live-llm",
        }

    port = _free_port()
    live_model = os.environ.get("OPENSQUILLA_LIVE_LLM_MODEL", "").strip()
    session_key = f"live-e2e:{int(time.time() * 1000)}"
    turns_spec = [
        {
            "message": "端到端验证第一轮：请用一句中文短句回复，包含 E2E_OK_1，不要调用工具。",
            "intent": "new_chat",
        },
        {
            "message": "继续上一轮，请用一句中文短句回复，包含 E2E_OK_2，不要调用工具。",
            "intent": "continue",
        },
        {
            "message": (
                "不对，你没理解我的意思。"
                "请重新回答一句中文短句，包含 E2E_OK_3，不要调用工具。"
            ),
            "intent": "continue",
        },
    ]
    with tempfile.TemporaryDirectory(
        prefix="opensquilla-router-live-e2e-",
        ignore_cleanup_errors=True,
    ) as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / "live-config.toml"
        turn_log_dir = tmp_path / "turn-calls"
        _write_live_gateway_config(config_path, live_model)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = str(config_path)
        env["OPENSQUILLA_STATE_DIR"] = str(tmp_path / "state")
        env["OPENSQUILLA_MEMORY_DREAM_DISABLED"] = "1"
        env["OPENSQUILLA_SANDBOX_SANDBOX"] = "false"
        env["OPENSQUILLA_SANDBOX_SECURITY_GRADING"] = "false"
        env["OPENSQUILLA_TURN_CALL_LOG"] = "1"
        env["OPENSQUILLA_TURN_CALL_LOG_DIR"] = str(turn_log_dir)

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "opensquilla.cli.main",
                "gateway",
                "run",
                "--port",
                str(port),
                "--bind",
                "127.0.0.1",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        turns: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        health: dict[str, Any] | None = None
        error: str | None = None
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=1)
                    error = f"gateway exited early with code {proc.returncode}: {stderr or stdout}"
                    break
                try:
                    health = _read_json(f"http://127.0.0.1:{port}/health")
                    break
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                    time.sleep(0.25)
            if health is None and error is None:
                error = "gateway did not become healthy before timeout"

            assistant_count = 0
            if error is None:
                for index, spec in enumerate(turns_spec, start=1):
                    accepted = _post_json(
                        f"http://127.0.0.1:{port}/api/chat",
                        {
                            "sessionKey": session_key,
                            "message": spec["message"],
                            "intent": spec["intent"],
                        },
                        timeout=10.0,
                    )
                    assistant, history, turn_error = _wait_for_assistant_reply(
                        port=port,
                        session_key=session_key,
                        previous_assistant_count=assistant_count,
                    )
                    if turn_error:
                        error = f"turn {index} failed: {turn_error}"
                        break
                    assistant_count += 1
                    turns.append(
                        {
                            "index": index,
                            "accepted": accepted,
                            "assistant_text": str((assistant or {}).get("text", "")).strip(),
                            "history_message_count": len((history or {}).get("messages", [])),
                        }
                    )
                if error is None:
                    usage = _read_json(f"http://127.0.0.1:{port}/api/usage", timeout=5.0)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            stdout_tail = (proc.stdout.read() if proc.stdout else "")[-2000:]
            stderr_tail = (proc.stderr.read() if proc.stderr else "")[-2000:]
            turn_call_records = _read_turn_call_records(turn_log_dir)

    llm_responses = [
        r
        for r in turn_call_records
        if r.get("session_key") == session_key and r.get("kind") == "llm_response"
    ]
    context_summary = _summarize_llm_request_context(
        turn_call_records,
        session_keys={session_key},
    )
    ok = (
        error is None
        and len(turns) == len(turns_spec)
        and all(turn.get("assistant_text") for turn in turns)
        and int(usage.get("totalTokens", 0) or 0) > 0
        and len(llm_responses) >= len(turns_spec)
    )
    return {
        "name": "opensquilla_gateway_live_llm_chat",
        "ok": ok,
        "session_key": session_key,
        "model": live_model,
        "health": health or {},
        "turns": turns,
        "usage": usage,
        "llm_response_count": len(llm_responses),
        "llm_response_models": [
            ((record.get("payload") or {}).get("usage") or {}).get("model")
            for record in llm_responses
        ],
        "llm_request_context_summary": context_summary,
        "model_override": live_model or None,
        "tier_models": _live_tier_model_map(live_model),
        "error": error,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }


def _live_case_turn_specs(case: dict[str, Any]) -> list[dict[str, str]]:
    if "turns" not in case:
        return [
            {
                "message": str(case["message"]),
                "intent": "new_chat",
            }
        ]
    specs: list[dict[str, str]] = []
    for index, turn in enumerate(case["turns"]):
        specs.append(
            {
                "message": str(turn["message"]),
                "intent": "new_chat" if index == 0 else "continue",
            }
        )
    return specs


def _live_router_case_suite_scenario() -> dict[str, Any]:
    api_key_set = bool(os.environ.get("OPENROUTER_API_KEY"))
    if not api_key_set:
        return {
            "name": "opensquilla_gateway_live_router_case_suite_50",
            "ok": False,
            "error": "OPENROUTER_API_KEY is required for --live-llm",
        }

    port = _free_port()
    live_model = os.environ.get("OPENSQUILLA_LIVE_LLM_MODEL", "").strip()
    live_cases = [
        {"kind": "single_turn", **case} for case in SINGLE_TURN_ROUTER_CASES
    ] + [{"kind": "dialogue", **case} for case in DIALOGUE_ROUTER_CASES]

    with tempfile.TemporaryDirectory(
        prefix="opensquilla-router-live-cases-",
        ignore_cleanup_errors=True,
    ) as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / "live-config.toml"
        turn_log_dir = tmp_path / "turn-calls"
        _write_live_gateway_config(config_path, live_model)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = str(config_path)
        env["OPENSQUILLA_STATE_DIR"] = str(tmp_path / "state")
        env["OPENSQUILLA_MEMORY_DREAM_DISABLED"] = "1"
        env["OPENSQUILLA_SANDBOX_SANDBOX"] = "false"
        env["OPENSQUILLA_SANDBOX_SECURITY_GRADING"] = "false"
        env["OPENSQUILLA_TURN_CALL_LOG"] = "1"
        env["OPENSQUILLA_TURN_CALL_LOG_DIR"] = str(turn_log_dir)

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "opensquilla.cli.main",
                "gateway",
                "run",
                "--port",
                str(port),
                "--bind",
                "127.0.0.1",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        health: dict[str, Any] | None = None
        usage: dict[str, Any] = {}
        cases: list[dict[str, Any]] = []
        error: str | None = None
        usage_error: str | None = None
        try:
            health, error = _wait_for_gateway_health(proc, port)
            if error is None:
                for case_index, case in enumerate(live_cases, start=1):
                    session_key = (
                        f"live-case:{case_index:02d}:{case['id']}:"
                        f"{int(time.time() * 1000)}"
                    )
                    assistant_count = 0
                    turns: list[dict[str, Any]] = []
                    case_error: str | None = None
                    for turn_index, turn_spec in enumerate(_live_case_turn_specs(case), start=1):
                        try:
                            accepted = _post_json(
                                f"http://127.0.0.1:{port}/api/chat",
                                {
                                    "sessionKey": session_key,
                                    "message": turn_spec["message"],
                                    "intent": turn_spec["intent"],
                                },
                                timeout=10.0,
                            )
                            assistant, history, turn_error = _wait_for_assistant_reply(
                                port=port,
                                session_key=session_key,
                                previous_assistant_count=assistant_count,
                            )
                        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                            accepted = {}
                            assistant = None
                            history = None
                            turn_error = str(exc)
                        if turn_error:
                            case_error = f"turn {turn_index} failed: {turn_error}"
                            turns.append(
                                {
                                    "index": turn_index,
                                    "intent": turn_spec["intent"],
                                    "accepted": accepted,
                                    "assistant_text": "",
                                    "history_message_count": len(
                                        (history or {}).get("messages", [])
                                    ),
                                    "ok": False,
                                    "error": turn_error,
                                }
                            )
                            break
                        assistant_count += 1
                        assistant_text = str((assistant or {}).get("text", "")).strip()
                        turns.append(
                            {
                                "index": turn_index,
                                "intent": turn_spec["intent"],
                                "accepted": accepted,
                                "assistant_text": assistant_text,
                                "history_message_count": len(
                                    (history or {}).get("messages", [])
                                ),
                                "ok": bool(assistant_text),
                            }
                        )

                    case_ok = bool(turns) and case_error is None and all(
                        turn["ok"] for turn in turns
                    )
                    cases.append(
                        {
                            "id": case["id"],
                            "kind": case["kind"],
                            "category": case["category"],
                            "session_key": session_key,
                            "ok": case_ok,
                            "turn_count": len(_live_case_turn_specs(case)),
                            "assistant_reply_count": sum(1 for turn in turns if turn["ok"]),
                            "turns": turns,
                            "error": case_error,
                        }
                    )
                if error is None:
                    try:
                        usage = _read_json(f"http://127.0.0.1:{port}/api/usage", timeout=30.0)
                    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                        usage_error = str(exc)
        finally:
            stdout_tail, stderr_tail = _stop_gateway(proc)
            turn_call_records = _read_turn_call_records(turn_log_dir)

    suite_session_keys = {case["session_key"] for case in cases}
    llm_responses = [
        record
        for record in turn_call_records
        if record.get("session_key") in suite_session_keys and record.get("kind") == "llm_response"
    ]
    context_summary = _summarize_llm_request_context(
        turn_call_records,
        session_keys=suite_session_keys,
    )
    if int(usage.get("totalTokens", 0) or 0) <= 0:
        usage = _usage_from_llm_responses(llm_responses)
    turn_count = sum(case["turn_count"] for case in cases)
    assistant_reply_count = sum(case["assistant_reply_count"] for case in cases)
    ok = (
        error is None
        and len(cases) == 50
        and turn_count >= 50
        and assistant_reply_count == turn_count
        and len(llm_responses) >= turn_count
        and int(usage.get("totalTokens", 0) or 0) > 0
        and all(case["ok"] for case in cases)
    )
    return {
        "name": "opensquilla_gateway_live_router_case_suite_50",
        "ok": ok,
        "case_count": len(cases),
        "single_turn_count": len(SINGLE_TURN_ROUTER_CASES),
        "dialogue_count": len(DIALOGUE_ROUTER_CASES),
        "turn_count": turn_count,
        "assistant_reply_count": assistant_reply_count,
        "llm_response_count": len(llm_responses),
        "llm_request_context_summary": context_summary,
        "model_override": live_model or None,
        "tier_models": _live_tier_model_map(live_model),
        "health": health or {},
        "usage": usage,
        "usage_error": usage_error,
        "cases": cases,
        "error": error,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }


async def _run_router_scenarios() -> list[dict[str, Any]]:
    return [
        await _route_case_suite_scenario(),
        await _route_class_coverage_scenario(),
        await _continuous_dialogue_scenario(),
        await _forced_recent_history_scenario(),
        await _complaint_upgrade_scenario(),
    ]


async def _run_all(skip_gateway: bool, live_llm: bool) -> dict[str, Any]:
    scenarios = await _run_router_scenarios()
    if skip_gateway:
        scenarios.append({"name": "opensquilla_gateway_startup", "ok": True, "skipped": True})
    else:
        scenarios.append(_gateway_startup_scenario())
    if live_llm:
        scenarios.append(_live_llm_chat_scenario())
        scenarios.append(_live_router_case_suite_scenario())
    return {
        "overall_ok": all(item.get("ok") is True for item in scenarios),
        "scenarios": scenarios,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    parser.add_argument(
        "--skip-gateway",
        action="store_true",
        help="run only router scenarios without starting opensquilla gateway",
    )
    parser.add_argument(
        "--live-llm",
        action="store_true",
        help="also call a real LLM through /api/chat; requires OPENROUTER_API_KEY",
    )
    args = parser.parse_args()

    if args.json:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            load_env(REPO_ROOT)
            report = asyncio.run(_run_all(skip_gateway=args.skip_gateway, live_llm=args.live_llm))
        noise = captured.getvalue()
        if noise:
            print(noise, file=sys.stderr, end="")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        load_env(REPO_ROOT)
        report = asyncio.run(_run_all(skip_gateway=args.skip_gateway, live_llm=args.live_llm))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
