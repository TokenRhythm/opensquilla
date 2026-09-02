# 可行性分析 — OpenSquilla 能否"以路由为内核，其余皆插件化"

> 分析日期：2026-08-24 | 对象：OpenSquilla main@4e48f9b56（v0.5.3）
> 参照系：DSH cordis 万物插件化（deepseek-harness-analysis/s5-plugins-everything-is-a-plugin.md）、pi 扩展模型（pi-analysis/模块化功能借鉴参考.md）
> 目标形态：用户 AGENTOS 设计（融合路由与子代理_架构草稿.md：c3 决策档 = 直接答/融合/拆解下发三模式）
> 方法：参照模型提炼 × OpenSquilla 本轮深挖实证（16/17/18 号文档 + 本次定向取证）。所有 OpenSquilla 侧结论均有本轮 文件:行号 证据。

---

## 0. 结论先行

**可行，但必须拆开说：**
- ✅ **"其他都可以插件化"**——OpenSquilla 的接缝成熟度远超预期，六个子系统已有真接口，管线形态天然就是 pi 式函数链中间件；
- ⚠️ **"以路由为主（内核化）"**——路由现在只是回合前管线里的一个 step（ albeit 104KB 的巨型 step），不是决策枢纽。内核化的正确姿势不是"重写"，而是**升格**：给路由输出增加 `execution_mode` 维度，让执行模式（直答/融合/拆解下发）成为第一批可注册插件；
- ❌ **"像 DSH 那样全面 cordis 化"**——不建议作为目标。内核原语层（fiber 可逆注册/patch 合成树）改造成本极高，且与上游 164 commits/月的演进速度正面冲突。

---

## 1. 参照模型的最小要件提炼

从两个参照系中提取"插件化"的**充分要件**（而非全部机制）：

| 要件 | DSH 的实现 | pi 的实现 | 本质 |
|---|---|---|---|
| 决策协商点 | waterfall 事件（不调 next() 即否决） | emitContext 可改参数 + cancel 短路 | 插件是**参与者**不是观察者 |
| 可逆注册 | fiber.effect 登记 disposer，卸载自动逆卷绕 | —（pi 无 HMR 需求） | 贡献物生命周期受控 |
| 声明式装配 | 四层 patch 合成（bundle→profile→home→overlay） | 三层配置覆盖链 base→user→plugin | 装配即数据，可审计可替换 |
| 依赖纪律 | 只准依赖 Service Definition 不准依赖 Provider | protocol→ai→agent→app 严格单向 | 换 Provider 即换世界 |
| 两阶段生命周期 | epoch 门控（依赖 ACTIVE 才 LOADING） | 声明期 pending queue / 绑定期 bindCore | 解决"插件加载时依赖未创建" |
| 少而精的事件粒度 | 全事件瀑布协商 | 仅 5 个钩子 | 插件作者不迷路 |

**关键洞察**：DSH 的内核其实很小（9 个文件的 context/events/fiber），它的"万物插件"是靠**装配层的声明式合成**撑起来的，而不是靠内核功能多。pi 更激进——内核就是一个 runner + pending queue。

## 2. OpenSquilla 现状盘点：真接缝 vs 伪接缝 vs 硬融合

### 2.1 已经是真接缝的（插件化完成度出乎意料地高）

| 子系统 | 现有接缝 | 证据 | 对照参照系的缺口 |
|---|---|---|---|
| Provider 层 | `LLMProvider` 统一协议 + 工厂创建 + `resolve_failover_chain` 故障转移链 + Ensemble 包裹器 | 10-stage1-4 P2/P3（confirmed）；provider/ 平铺适配 | 缺第三方包发现（entry-points 无）；工厂内部 if/else 需读源码加分支 |
| 工具系统 | registry + builtin 副作用注册；工具分 RiskTier；审批门控 veto 性质 | S4-P6/S2；tools/builtin/web.py `@tool(name="web_search")` | 同名覆盖语义未定义；无来源优先级链 |
| 通道系统 | Channel 协议 + `ChannelCapabilityProfile` 能力档案 + 平台能力矩阵 | channels/contract.py:176-607（17 号 §1 亲证） | 这已经是 DSH 三角色模式的雏形 |
| 技能系统 | 六层优先级栈（Extra→Bundled→Managed→Personal→Project→Workspace），同名高层覆盖低层 | S4-P7 confirmed | **这就是 OpenSquilla 事实上的用户级插件系统**，且自带覆盖语义与来源分层 |
| RPC 面 | `@_d.method("name", scope=...)` 装饰器自注册 + scope 权限标注 | rpc_onboarding.py 等 31 方法（17 号亲证） | 注册表已存在，只差"外部包可挂载" |
| 回合前管线 | `TurnStep = Callable[[TurnContext], Awaitable[TurnContext]]` 函数链 + run_pipeline 顺序执行 | pipeline.py:19/:64（本次亲证） | **形态就是 pi 的 emitContext 中间件**；缺的只是组装方式（见 2.2） |
| MCP 进出站 | 出站客户端导入外部工具 + 入站服务器暴露会话 | S2 模块映射 | 天然跨进程插件通道 |

### 2.2 伪接缝（形态对但组装死）

**回合管线的组装是命令式硬编码**——runtime.py:9128-9157（本次逐行亲证）：

```python
pipeline_steps = [resolve_model]
if not restricted_tool_boundary: append(apply_vision_followup_gate)
extend([_bounded_apply_squilla_router, observe_reasoning_hint])
if not planning_turn and ...: extend([meta_resolution, enforce_coding_mode])
...
insert(-4, meta_command_launch)   # ← 用魔法下标往回插
```

步函数 import 期固定、顺序由 if/else 分支决定、`insert(-4)` 这种位置魔数——**这正是 DSH 用 patch 合成树消灭的东西**。步骤本身是干净的中间件，但装配不可声明、不可审计、不可由第三方增删。
另注：路由 step 被特殊包裹 `_bounded_apply_squilla_router`（独立线程 + asyncio.run + 5s 超时，runtime.py:8816-8838）——路由已被当成"需要隔离的特殊公民"，这是内核化的现成抓手。

### 2.3 硬融合区（真正的改造难点）

| 位置 | 问题 | 规模 |
|---|---|---|
| `engine/skills/../steps/squilla_router.py` | 路由逻辑本体：分类器+闸门+自学习闭环全在一个 step 文件里；子代理 bypass 是内联 hack（`:subagent:` 字符串检测钉档，AGENTOS 草稿 §2 记录） | 104KB（较基线再膨胀） |
| `engine/agent.py` | 状态机+工具循环+预算治理+压缩触发一体 | 1.17MB（A1 信号持续恶化） |
| `engine/runtime.py` | 管线组装+上下文渲染+辅助门控混装 | 585KB |
| `gateway/boot.py` | 命令式装配单体（build_services 千行函数族） | 235KB |

**核心判断：以路由为内核的最大障碍不是路由不够强，而是"路由的输出维度太少"。** 当前 RoutingDecision ≈ {tier, provider, model}——它只回答"用哪个脑子"，不回答"用什么方式干"（直答/融合/拆解下发）。你的 AGENTOS 草稿 §0 已经诊断出这一点："c3 不是固定融合路由而是决策档……可选尚未实现"。

## 3. 改造路线分级

### L1 · 路由内核化（推荐主攻，与 AGENTOS v2 直接接轨）

**目标**：路由升格为"执行模式决策总线"，三种执行模式成为第一批策略插件。

```
TurnContext → [既有 steps...] → apply_squilla_router
                                   ↓ RoutingDecision += execution_mode ∈ {direct, ensemble, decompose}
                              ┌────┼────────┐
                        DirectExec  EnsembleExec  DecomposeExec   ← 新接口 ExecutionStrategy
                        （现状直连）（现有 ensemble 包装）（v2 新增：规约树→确定性 spawn→聚合）
```

具体落点（全部锚定本轮亲证）：
1. `engine/steps/squilla_router.py` 的 RoutingDecision 增加 `execution_mode` 字段；c3 + `ensemble_enabled` 从"静态常开"改为三选一判定的一个分支（对应你草稿 §5 开放问题 1 的机制位）；
2. 新建 `engine/exec_modes/` 包：`ExecutionStrategy` 协议（Definition）+ 三个内置 Provider（Direct 复用 selector 直绑、Ensemble 复用现有 ensemble.py、Decompose 对接你 v1 的 task-decomposer 技能产出的规约树）；
3. runtime.py:9128 组装处把 `_bounded_apply_squilla_router` 后的分派逻辑改为查策略注册表；
4. 子代理 bypass hack（squilla_router.py:1903-1941）收编为 DecomposeExec 的 ingress 正规路径。

工作量估计：新包 ~500 行 + 三处锚点修改。**不碰 agent.py 状态机**——执行模式分派发生在管线产出 RoutingDecision 之后、agent 循环消费 provider 之前，恰好是现有 ensemble 包裹器已经占据的缝。

### L2 · 管线装配声明化（中成本，独立可做）

把 2.2 的硬编码清单搬进数据：
- 步骤注册表：`{step_id: TurnStep}` + 条件谓词（planning/restricted/planning_turn 已是现成谓词）
- 装配清单三层覆盖：内置 bundle 清单 ← 用户 opensquilla.toml `[pipeline]` overlay ← 测试/实验 overlay（照抄 pi 的 base→user→plugin 合并链，Python 实现就是有序 dict merge）
- `insert(-4)` 类魔数消灭：清单条目带 `before/after` 定位符（DSH patch 按 id 定位的简化版）
- 收益：实验性步骤（你 8/22 的 max_tier clamp 实验、思维强度对照）不再需要改 runtime.py，配置即可插拔；决策日志里 pipeline_steps 记录天然成为装配审计。

### L3 · 全面 cordis 化（不建议，或远期仅取半件）

- ❌ fiber 可逆注册 + HMR：Python 无等价物；诚实评估只能做到"进程级卸载"（注册表快照 + 重启生效），而这正是 OpenSquilla 现有 config 热更新契约（17 号 §6）已经覆盖的语义——重复建设。
- ⚠️ Context-as-Proxy 服务解析：TurnContext 已经是显式字段 + metadata dict 的混合体；改成 Proxy 解析收益低于 mypy 损失。
- ✅ 唯一值得远期偷师：DSH 的"REAL 组合测试"门禁——产品可见插件必须经真实装配验证。可直接落为：新增 exec_mode 必须带一条走完 run_pipeline 的集成测试。

### 明确不做清单（用你自己总结的 pi 反模式当避雷针）

1. **不建第二套 ensemble/compaction**（双份 Compaction 教训）——DecomposeExec 必须消费现有 ensemble/spawn 基础设施，不平行造轮子；
2. **不建 compat 大泥球**——旧路径（c3 静态常开）迁移完成后删，不留兼容层超过一个版本；
3. **不为插件化而插件化**——memory/session 存储层（18 号文档）刚经历过争用调参，其接缝（MemoryStore 接口、SessionStorage 单类）保持现状，不动。

## 4. 与上游演进的共存策略（决定成败的元问题）

基线 f87fed4b → HEAD 才三周就 164 commits / src 341 文件（16 号文档）。深度重构的真实成本不是一次性工时，而是**永久性合并税**。对策：

| 策略 | 适用 |
|---|---|
| **新模块承载增量**（exec_modes/ 全新目录） | L1/L2 全部——新文件不参与上游合并冲突 |
| **锚点修改最小化**（每处 ≤20 行，登记清单） | RoutingDecision 加字段、runtime.py 组装处查表、bypass 收编三处 |
| **锚点登记制**（学 DSH vendor/README.md 纪律） | 每个 锚点:行号:意图 记入本分析目录，升级后逐条复核 |
| **技能层兜底** | 你 v1 的 task-decomposer 零代码路径继续有效——SKILL.md 是随上游演进最稳的载体 |

## 5. 判定汇总

| 命题 | 判定 | 置信度 |
|---|---|---|
| OpenSquilla 其余子系统可以插件化 | ✅ 成立——六个真接缝已在，缺的是声明式装配与第三方发现机制 | high（本轮逐子系统亲证） |
| 以路由为内核 | ⚠️ 有条件成立——须先把路由从"tier 决策器"升格为"执行模式决策总线"；这恰是你 AGENTOS 草稿 c3 决策档的工程化表达 | high |
| 像 DSH 一样全面 cordis 化 | ❌ 不建议——内核原语改造成本与上游漂移速度不成比例；取其装配思想（patch 合成）与门禁思想（REAL 组合测试）即可 | high |
| 落地顺序 | L1 路由内核化（~500 行新代码+3 锚点）→ L2 装配声明化 → L3 仅取测试门禁 | — |

## 6. 未完全核验项

- `provider/selector.py` 工厂内部结构未在本轮重读（沿用 10 号 checkpoint 的 confirmed 结论）——L1 动手前需一次定向复核。
- squilla_router.py 1903-1941 bypass 段引用自 AGENTOS 草稿记录（pr/custom-provider 分支），main 分支是否已含该提交未核对（git log 显示 4e48f9b56 含 routing experiments，倾向已合入，unverified）。
- 设想改造.md（1.4MB）未读取——若其中含与本分析冲突的既定决策，以该文件为准需另行对齐。
