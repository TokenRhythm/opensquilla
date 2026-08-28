# -*- coding: utf-8 -*-
"""子代理小路由 (B方案) 冒烟脚本 — 验证新代码对外契约。

覆盖链路：
1. sessions_spawn 工具 schema 含 max_tier
2. AgentSubagentDefaults / AgentsDefaults 解析 max_tier
3. _resolve_subagent_policy 合并 per-agent + global 的 max_tier
4. build_subagent_route_envelope 携带 router_max_tier
5. ToolContext.router_max_tier 透传
6. 路由 _clamp_decision_to_max_tier 生效（含 image_safe）
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from opensquilla.gateway.config import AgentSubagentDefaults, GatewayConfig
from opensquilla.gateway.routing import (
    RouteEnvelope,
    build_subagent_route_envelope,
    tool_context_from_envelope,
)
from opensquilla.engine.routing import RoutingDecision
from opensquilla.engine.steps.squilla_router import _clamp_decision_to_max_tier
from opensquilla.tools.builtin.sessions import _resolve_subagent_policy

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def main() -> int:
    print("== 1. sessions_spawn 工具 schema ==")
    from opensquilla.tools.registry import ToolRegistry
    try:
        reg = ToolRegistry()
        # 工具注册在模块导入时通过 @tool 装饰器写入全局 registry；
        # 直接构造 ToolRegistry 拿不到已注册项，改用默认注册表。
        import opensquilla.tools as tools_pkg
        default_registry = getattr(tools_pkg, "registry", None)
        rt = None
        if default_registry is not None:
            rt = default_registry.get("sessions_spawn")
        if rt is None:
            # 尝试通过内置工具模块的注册表
            from opensquilla.tools.builtin import sessions as sessions_tool
            # 工具装饰器会把 spec 挂在函数对象上
            spec_attr = getattr(sessions_tool.sessions_spawn, "_tool_spec", None) \
                or getattr(sessions_tool.sessions_spawn, "spec", None)
            if spec_attr is not None:
                params = getattr(spec_attr, "parameters", None) or {}
                check("schema 含 max_tier", "max_tier" in params, str(list(params.keys())[:10]))
            else:
                # 最后尝试：直接读装饰器元数据
                import inspect
                sig = inspect.signature(sessions_tool.sessions_spawn)
                check("签名含 max_tier", "max_tier" in sig.parameters, str(list(sig.parameters)))
        else:
            params = getattr(rt.spec, "parameters", None) or {}
            check("schema 含 max_tier", "max_tier" in params, str(list(params.keys())[:10]))
    except Exception as exc:
        check("schema 含 max_tier", False, f"注册表查询失败: {exc}")

    print("== 2. AgentSubagentDefaults 解析 max_tier ==")
    try:
        cfg = AgentSubagentDefaults(max_tier="c1")
        check("解析 c1", cfg.max_tier == "c1", str(cfg.max_tier))
    except Exception as exc:
        check("解析 c1", False, str(exc))

    print("== 3. _resolve_subagent_policy 合并 max_tier ==")
    async def _policy() -> None:
        class _Mgr:
            async def get_agent_config(self, agent_id: str):
                return {"subagents": {"max_tier": "c1"}}

        # global 默认 (来自 GatewayConfig.agents_defaults.subagents)
        gcfg = GatewayConfig(
            agents_defaults={"subagents": {"max_tier": "c2"}},
            subagents={},
        )
        # monkeypatch _gateway_config
        import opensquilla.tools.builtin.sessions as st
        old = st._gateway_config
        st._gateway_config = gcfg
        try:
            merged = await _resolve_subagent_policy(_Mgr(), "main")
            # per-agent wins over global
            check("per-agent max_tier=c1 覆盖 global c2", merged.get("max_tier") == "c1", str(merged))
            merged2 = await _resolve_subagent_policy(_Mgr(), "other")
            check("无 per-agent 时用 global c2", merged2.get("max_tier") == "c2", str(merged2))
        finally:
            st._gateway_config = old

    asyncio.run(_policy())

    print("== 4. build_subagent_route_envelope 携带 router_max_tier ==")
    env = build_subagent_route_envelope(
        session_key="agent:main:sub:test",
        parent_session_key="agent:main:main",
        max_tier="c1",
    )
    check("envelope metadata 含 router_max_tier", env.metadata.get("router_max_tier") == "c1", str(env.metadata))
    env2 = build_subagent_route_envelope(
        session_key="agent:main:sub:test2",
        parent_session_key="agent:main:main",
        max_tier="  c2  ",
    )
    check("strip 空白", env2.metadata.get("router_max_tier") == "c2", str(env2.metadata.get("router_max_tier")))
    env3 = build_subagent_route_envelope(
        session_key="agent:main:sub:test3",
        parent_session_key="agent:main:main",
    )
    check("未传 max_tier 不注入", "router_max_tier" not in env3.metadata, str(env3.metadata))

    print("== 5. ToolContext 透传 router_max_tier ==")
    tc = tool_context_from_envelope(env)
    check("ToolContext.router_max_tier=c1", getattr(tc, "router_max_tier", None) == "c1", str(getattr(tc, "router_max_tier", None)))
    tc3 = tool_context_from_envelope(env3)
    check("无 max_tier 时 None", getattr(tc3, "router_max_tier", None) is None, str(getattr(tc3, "router_max_tier", None)))

    print("== 6. 路由 clamp 生效 ==")
    tiers = {
        "c0": {"model": "model-c0", "supports_image": True},
        "c1": {"model": "model-c1", "supports_image": False},
        "c2": {"model": "model-c2", "supports_image": True},
        "c3": {"model": "model-c3", "supports_image": False},
        "image_model": {"model": "model-image", "supports_image": True, "image_only": True},
    }
    md: dict = {}
    # 文本 c3 -> c1
    r1 = _clamp_decision_to_max_tier(
        RoutingDecision(tier="c3", model="model-c3", confidence=0.9, source="classify"),
        tiers, "c1", metadata=md,
    )
    check("文本 c3 clamp 到 c1", r1.tier == "c1" and r1.model == "model-c1", f"{r1.tier}/{r1.model}")
    check("telemetry clamped", md.get("router_max_tier_clamped") is True, str(md))
    # 图像 c2 -> image_model (c1 不支持视觉)
    md2: dict = {}
    r2 = _clamp_decision_to_max_tier(
        RoutingDecision(tier="c2", model="model-c2", confidence=1.0, source="image_route"),
        tiers, "c1", metadata=md2, image_safe=True,
    )
    check("图像 c2 豁免到 image_model", r2.tier == "image_model" and r2.model == "model-image", f"{r2.tier}/{r2.model}")
    check("image exempt telemetry", md2.get("router_max_tier_image_exempt") is True, str(md2))
    # 图像 c0 (支持视觉) -> 不 clamp
    md3: dict = {}
    r3 = _clamp_decision_to_max_tier(
        RoutingDecision(tier="c0", model="model-c0", confidence=1.0, source="image_route"),
        tiers, "c1", metadata=md3, image_safe=True,
    )
    check("图像 c0 不 clamp", r3.tier == "c0", f"{r3.tier}")
    # 无天花板 -> 不 clamp
    r4 = _clamp_decision_to_max_tier(
        RoutingDecision(tier="c3", model="model-c3", confidence=0.9, source="classify"),
        tiers, None, metadata={},
    )
    check("无天花板不 clamp", r4.tier == "c3", f"{r4.tier}")

    print(f"\n结果: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
