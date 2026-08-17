# Router Dynamic 单模型路由最小改造方案

- 状态：已实现，待提交/合并
- 基线：`47e9dd0f9d0c1decb7d36f86dba066f3d1f3d2eb`
- Worktree：`/home/codex/code/opensquilla-dev-20260814`
- 更新日期：2026-08-17

## 1. 目标

保持现有 `selection_mode=router_dynamic`，复用已经存在的 `llm_ensemble.mode` 作为执行拓扑开关：

```toml
[llm_ensemble]
enabled = true
selection_mode = "router_dynamic"

# 默认值，执行现有多模型融合链路
mode = "multiple"

# 改为 single 时，选择排名第一的 proposer 直接执行
# mode = "single"
```

`selection_mode` 始终保持 `router_dynamic`。这里只扩展现有 `mode` 的允许值，不新增配置字段；
默认值改为 `multiple`，旧配置值会在输入边界自动迁移。

## 2. 行为

### 开关关闭

完全执行现有链路：

```text
Task Analyzer
  -> proposer/aggregator ranking
  -> 多 proposer
  -> quorum
  -> aggregator 融合
```

### 开关开启

```text
Task Analyzer
  -> proposer 原有评分排序
  -> 取第一个合格 proposer
  -> 普通 Provider / Agent Loop
```

开启后：

- 不选择其他 proposer；
- 不计算 quorum；
- 不选择或调用 aggregator；
- 不构造 candidate bundle 或融合 prompt；
- 整个 turn 始终使用同一个 generation model；
- Agent 工具循环继续正常执行。

“第一个 proposer”指 aggregator 和多 proposer diversity 介入前，按现有 proposer 基础评分排序得到的
Top-1。不能先完整执行 P/A 排名再读取 `selected_P[0]`，否则仍会依赖 aggregator。

## 3. 最小代码改动

### 3.1 配置

在 `src/opensquilla/gateway/config.py::LlmEnsembleConfig` 中扩展现有字段：

```python
mode: Literal["multiple", "single"] = "multiple"
```

约束：

- `single` 仅在 `enabled=true` 且 `selection_mode=router_dynamic` 时允许；
- 默认值仍为 `multiple`；
- `b5_fusion -> multiple`、`router_single -> single` 仅作为兼容输入；
- TOML、环境变量和 RPC 读取旧值后统一归一化，对外序列化只输出新值。

配置期校验按执行拓扑隔离：

- `multiple` 原有校验及其执行顺序保持不变；
- `single` 跳过 aggregator 输出预算、custom lineup 的 aggregator 数量、
  router-dynamic aggregator recovery chain 等仅融合链路需要的交叉校验；
- 字段的类型和取值范围校验继续执行，proposer 输出预算等 proposer 校验也继续执行；
- 被 single 分支忽略的 aggregator 字段仍原样保留；配置以 `multiple` 重新加载时，
  再按融合规则校验。

### 3.2 排名

先执行 direct execution 硬过滤，再使用现有 proposer 基础评分排序并增加停止点：

```python
eligible = direct_execution_filter(proposer_candidates)
ranked_proposers = rank_with_existing_proposer_score(eligible)
first_proposer = ranked_proposers[0]
```

single 分支在此返回，不再执行：

- proposer 集合扩展；
- diversity/coverage；
- aggregator feasibility；
- aggregator ranking；
- quorum/backup 计算。

不新增评分公式，继续使用现有 proposer 的质量、可靠性、成本和延迟评分。

direct execution 硬过滤要求：

- registry `status=enabled`；
- 凭证和 deployment 可用；
- 支持本轮工具、输入模态和上下文；
- 排名阶段的健康状态可用。

选定 Top-1 后仍要在 dispatch 前重新检查健康状态；此时失败则零请求、直接失败，不改选下一模型。

### 3.3 Runtime

在 `src/opensquilla/engine/runtime.py` 的 `router_dynamic` 分支中读取冻结的 `mode`：

```python
if state.mode == "multiple":
    # 原融合代码保持不变
    provider = build_ensemble_provider_from_config(...)
elif state.mode == "single":
    first = select_first_ranked_proposer(...)
    provider = build_direct_provider(first)
```

single 分支必须早于 aggregator budget、candidate context 和 aggregator 可用性校验。

选中的 proposer 要作为普通 Provider 执行，不能构造成只有一个 proposer 的
`EnsembleProvider`，否则工具调用语义不正确。

### 3.4 执行规则

- Analyzer 和 generation 继续共用当前 turn 的 absolute deadline；
- single 分支不使用 proposer/aggregator timeout 或 quorum grace；
- 只允许同一选中模型的安全 retry；
- 不切换到第二 proposer、aggregator 或 V4 其他模型；
- 显式 per-turn `model=` 仍保持最高优先级，并直接跳过动态路由。

## 4. 原链路零影响要求

当 `mode` 缺省或为 `multiple` 时：

- 不调用 single selector；
- 原 `rank_models()` 调用和顺序不变；
- P/A、backup、quorum 和 timeout 不变；
- `EnsembleProvider` 不变；
- selection plan、trace、usage 和事件不变；
- 不新增 single metadata；
- 原运行调用图不变；配置枚举统一序列化为新规范值，新运行的 fingerprint 和 hot snapshot dump
  也使用新规范值；旧运行和旧配置通过输入别名保持兼容；
- 原测试结果必须完全一致。

实现时不要为了复用代码重构原融合分支。新增逻辑只放在明确的 `if` 分支内。

## 5. 必要测试

### 开关关闭

- 缺省值和显式 `mode="multiple"` 结果一致；
- single selector 被 mock 为抛异常时，原融合链路仍成功；
- 原 P/A、quorum、selection plan hash 和 trace golden 不变；
- 除配置枚举的新规范值外，原运行调用图、选择计划和 hot snapshot 行为不变；
- `model_dump`、public config 和新运行指纹只包含 `multiple` / `single`。

### 开关开启

- `enabled=false` 或 `selection_mode!=router_dynamic` 时拒绝 `mode=single`；
- aggregator 字段范围仍校验，但 aggregator 专属的融合交叉约束不阻断 single 配置加载；
- proposer 字段范围和交叉约束继续生效；
- 选择 proposer 基础排名 Top-1；
- raw Top-1 不满足工具/上下文要求时，选择过滤后合格池的 Top-1；
- aggregator selector/builder 被 mock 为抛异常时仍成功；
- 没有可用 aggregator 时仍能选择单模型；
- `candidate_max_chars=0`，且 fusion budget/context/aggregator-feasibility helper 被 mock 为抛异常时仍成功；
- 只 materialize 一个模型；
- 不产生 proposer fan-out、quorum 或 aggregator 请求；
- 工具调用能正常执行；
- 所有 Agent iteration 使用同一个模型；
- 没有 direct-eligible 模型时直接失败；已选模型的 dispatch 前健康检查失败时零请求且不重选。

## 6. 修改文件

生产代码：

- `src/opensquilla/gateway/config.py`
- `src/opensquilla/provider/ranking_router.py`
- `src/opensquilla/provider/ensemble.py`
- `src/opensquilla/engine/runtime.py`
- `src/opensquilla/engine/turn_runner/harness.py`
- `src/opensquilla/engine/turn_runner/prompt_assembler_stage.py`

测试：

- `tests/test_llm_ensemble_config.py`
- `tests/test_ranking_router.py`
- `tests/test_router_single_runtime.py`

本实现不修改 DRACO 执行逻辑、数据库、自学习或现有 ensemble 观测协议；此次配置术语重命名
已同步到 DRACO 配置输出和兼容读取：新输出使用规范值，旧值作为输入别名继续可读。

验证结果：router-single 专测 55 passed；provider ensemble + config 613 passed；ranking 271 passed。
另有 1 个既有 30 ms deadline 边界 flaky，目标单跑通过，且对应生产函数未被本改动修改，
不属于本功能问题。Ruff、py_compile 和 diff-check 均通过。

## 7. 验收标准

1. `mode=multiple` 时，除枚举序列化值外，原多模型融合链路行为不变。
2. `mode=single` 时，只选择 proposer Top-1，不选择 aggregator。
3. 整个 turn 只使用一个 generation model。
4. 普通 Agent 工具循环、usage 和 deadline 正常。
5. 将 `mode` 恢复为 `multiple` 后，下一 turn 立即恢复原融合链路。
