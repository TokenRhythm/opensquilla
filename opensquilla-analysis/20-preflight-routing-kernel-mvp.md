# 前置复核报告 — 路由内核化 MVP（纯读，未动代码）

> 日期：2026-08-24 | 性质：落地计划的前置取证（19 号文档 §L1 与后续讨论的 unverified 清偿）
> 结论用途：供主人裁决 MVP 方案。所有结论行级亲证。

---

## F1. 决策对象链：两级结构（比预想多一级）

| 对象 | 位置 | 字段 | 角色 |
|---|---|---|---|
| `RoutingDecision` | engine/routing/policy.py:107-113 | `{tier, model, confidence, source}` | 路由 step 的原始输出，极简 |
| `RoutePlan` | engine/route_plan.py:62-93 | frozen+slots：`{version, plan_id, turn_id, tier, provider, model, source, routing_applied, thinking, prompt_policy, fallback_chain, capabilities}` | TurnContext.route_plan 钉定的**不可变回合绑定**；`as_dict()` 进决策日志 |

**载体裁决输入**：`execution_mode` 放哪有两个选项——
- **A. `turn.metadata["execution_mode"]`**（推荐 MVP）：零 schema 变更，与既有 `ensemble_enabled/ensemble_wrap_skipped_reason` 等 metadata 同模式；
- B. RoutePlan 加字段（长期更干净，但 as_dict 进持久化决策日志，有观测兼容成本）→ 留 v2。

## F2. 两条核心路径的精确位置（全部在 `_run_pipeline` 一个方法内）

`_run_pipeline`（runtime.py:8734-9648，约 900 行）承担五重职责：管线组装(:9128-9156) → run_pipeline 执行(:9157) → 容量准入(:9158 起，注释明示 safety-critical 且故意在 fail-open 包装之外) → 路由模型回写 → 融合包裹 → 返回 `(turn, provider)`(:9648)。

- **单模型路径**：`:9395-9417` `apply_model_override(cloned_selector, turn.model, ...)` 把路由模型套上选择器得到 `provider`；
- **融合路径**：总闸 `:9438-9442`＝`provider is not None && (ensemble_globally_enabled || tier_ensemble_mode) && artifact_ensemble_bypass is None`；体 `:9443-9646`；成功即 `provider = ensemble_provider`(:9646)；**六类 wrap_skipped 降级分支**（:9471/:9488/:9514/:9526/:9547/:9556/:9631——selection_mode 不支持、凭据缺失、lineup 未就绪、dynamic 候选被阻等），全部降级回落 `fixed_provider`(:9643)。
- 激活状态写入 `turn.metadata`（ensemble_enabled / activation_source / tier_binding / selection_mode / routed_model_before_ensemble）。

## F3. bypass hack 状态（清偿 19 号 unverified）

**已合入 main**，且从实验分支的 1903-1941 移位为两处：
- squilla_router.py:**1239** `or ":subagent:" in ctx.session_key`
- squilla_router.py:**1809** `if ":subagent:" in ctx.session_key:`

## F4. 钩子插入点：两个候选（本报告最关键的裁决输入）

| | 早钩子 | **晚钩子（推荐）** |
|---|---|---|
| 位置 | :9157 run_pipeline 之后、融合闸 :9438 之前 | :9648 `return (turn, provider)` 之前 |
| 扩展拿到什么 | 未绑定 provider 的裸 turn | 完整 `(turn, provider)` + RouterKernelCaps |
| 内核路径影响 | 锚点需嵌进 fixed_baseline_ensemble/容量准入等守卫迷宫，复杂度高 | 单模型+融合路径**零 diff**（构造性保证行为不变） |
| 对扩展类型的适配 | provider 替换型可完全接管绑定 | provider 替换型可覆盖返回值；编排型（拆解下发）拿 caps 编排子代理，父回合 provider 本来就要正常绑定——语义刚好 |
| 锚点大小 | 大（穿插多处守卫） | **~5-15 行** |

**推荐晚钩子**。理由：在"内核两条路径零 diff"的硬约束下，晚钩子是唯一不嵌入守卫迷宫的位置；且拆解下发这类编排型扩展本来就需要父回合完成正常绑定（它自己还要用聚合模型说话）。

## F5. 新发现（影响方案判断的两件事）

1. **扩展其实分两类，接缝需求不同**：
   - *provider 替换型*（ensemble 同款）：换掉 LLMProvider 即改变执行方式——晚钩子直接支持；
   - *编排型*（拆解下发）：要在回合内 spawn 子代理并回收——晚钩子 + `caps.route_single/run_fusion` 回调可承载其地基，但完整的投递状态机/回收循环属协议 v2 范围，MVP 只证回调通。
2. **融合激活本身就是一个降级迷宫**（六类 skip 分支）——扩展注册表的 `should_activate` 必须把"激活失败回退内核默认"当作常态设计（与协议 v1 §7 优雅降级链一致），不能假设激活必成。

## F6. 修订后 MVP 的锚点预算（按用户红线版：内核两路径不可插件化）

| 锚点 | 位置 | 规模 |
|---|---|---|
| ① 决策输出 | squilla_router step 收口处写 `turn.metadata["execution_mode"]`（默认空=内核默认） | ~5 行 |
| ② 晚钩子 | runtime.py :9648 return 前查扩展注册表，命中则交扩展执行（注入 caps） | ~10-15 行 |
| 新目录 | `engine/exec_modes/`：base.py 协议+注册表(~60 行) + caps.py 内核能力包装(~60 行，包装不改逻辑) + 示例扩展(仅测试注册, ~40 行) | ~160 行 |
| 测试 | 行为等价黄金对照 + 示例扩展命中/回调两条 + 存量回归 | — |

**明确不做**（维持上一轮裁决）：Decompose 生产实现（等 v1 规约树 schema）、bypass hack 收编（与 mode 分派共存无冲突）、L2 声明式装配、agent.py/存储层触碰。

## F7. 清偿 19 号 unverified 对照

| 原 unverified 项 | 结果 |
|---|---|
| provider 工厂内部未重读 | ✅ 已亲证 provider 绑定收口在 _run_pipeline（selector_override.apply_model_override + build_ensemble_provider_from_config） |
| bypass hack 是否合入 main | ✅ 已合入，移位至 :1239/:1809 |
| （新增）融合触发条件精确位置 | ✅ :9438-9442 总闸 + 六类降级分支 |

## F8. 供主人裁决的两个方案参数

1. **mode 载体**：metadata（MVP 推荐）vs RoutePlan 字段（v2 再迁）？
2. **钩子位置**：晚钩子（推荐，理由 F4）vs 早钩子？

裁决后即可出正式落地计划（文件清单/diff 预估/测试清单/回滚点），过目后进 Stage 9。
