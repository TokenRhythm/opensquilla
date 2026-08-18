# 多模型路由独立服务设计（最小版）

状态：Proposed
基线：`feature/multi-llm-ensemble-routing2`，`47e9dd0f`

## 1. 设计结论

`model-router` 根据任务选择 proposer 和 aggregator。

- 请求只传 `task_query`、可选的 `request_context` 和 `user_profile`。
- 服务内部调用任务画像模型，生成 `task_profile`。
- `model_profile` 不通过 API 传递，由服务维护版本化 JSON 配置。
- 成功响应只返回 `proposers` 和 `aggregator`。
- thinking、quorum、重试、recovery、Canary、工具调用和模型融合留在 OpenSquilla。
- 候选分数、过滤原因、task profile、配置版本和哈希只保存在服务内部。

V1 只负责首次选择主 proposer 和主 aggregator，不返回 backup roster、aggregator fallback chain 或 task-specific thinking assignment。

## 2. 职责边界

| `model-router` | OpenSquilla |
| --- | --- |
| 校验 `task_query`、`request_context` 和 `user_profile` | 生成受限的任务上下文摘要和用户画像 |
| 调用任务画像模型 | conversation/session 生命周期 |
| 生成并校验 `task_profile` | 模型凭证和 provider 实例化 |
| 加载 ranking config 和 model profile JSON | thinking、quorum、重试和 recovery |
| 按 status、角色、能力和上下文过滤 | Canary 最终准入和执行期健康处理 |
| 可靠性、成本、延迟和用户偏好评分 | 工具、stream、usage、费用和模型融合 |
| 选择主 proposers 和主 aggregator | 最终答案与 session last route 更新 |

服务只持有任务画像模型的凭证，不持有 proposer 或 aggregator 的 API key，也不得调用生成模型。

## 3. API

### 3.1 创建路由

`POST /v1/routes`

请求头 `Idempotency-Key` 必填。同一个 key 配同一个 canonical request body 必须返回原终态响应；同一个 key 配不同 body 返回 `409`。

请求：

```json
{
  "task_query": "分析这个需求并给出实现方案",
  "request_context": "用户正在设计多模型路由服务，希望保持接口最小化。",
  "user_profile": {
    "permission": {},
    "preference": {
      "quality_latency_tradeoff": "balanced",
      "cost_sensitivity": "medium"
    },
    "history": {
      "positive_model_ids": [],
      "negative_model_ids": [],
      "feedback_count": 0
    }
  }
}
```

约束：

- `task_query` 必须是非空字符串，并受长度限制。
- `request_context` 是可选字符串，用于提供当前 query 所依赖的背景；缺省为空字符串，并受独立长度限制。
- `user_profile` 必须传入且必须是对象；没有个性化信息时传空对象 `{}`。
- 请求中不传 `task_profile`、`model_profile`、ranking config、候选模型、模型凭证或运行时健康账本。

`request_context` 由 OpenSquilla 从当前会话生成有界摘要，只保留理解本次 query 必需的信息。V1 不传完整 conversation、原始工具日志或完整 workspace 状态。

成功响应：

```json
{
  "proposers": [
    "openrouter:openai/gpt-5.6-sol",
    "openrouter:google/gemini-3.1-pro-preview"
  ],
  "aggregator": "openrouter:z-ai/glm-5.2"
}
```

模型标识统一为 `<provider>:<model-id>`。OpenSquilla 根据该标识解析本地 deployment、upstream provider 和凭证。

响应约束：

- `proposers` 至少包含一个模型，且不能重复。
- `aggregator` 只返回一个模型。
- 返回模型必须存在于服务的 model profile JSON，并满足对应角色要求。
- 正常响应不得加入 decision ID、backup、thinking、quorum、recovery、hash、trace 或 execution constraints。

### 3.2 错误

| HTTP | 含义 |
| --- | --- |
| 400 | `task_query`、`request_context` 或 `user_profile` 无效 |
| 409 | 同一幂等 key 对应不同请求 |
| 422 | 没有满足要求的 proposer 或 aggregator |
| 429 | 路由服务或任务画像模型暂时过载 |
| 503 | 配置、模型画像、任务画像模型或服务不可用 |
| 504 | 任务分析或路由决策超时 |

错误响应只返回简短的 `code` 和 `message`。OpenSquilla 按现有本地策略决定回退，不把回退计划放进路由响应。

## 4. 服务内部流程

1. 校验 `task_query`、`request_context`、`user_profile` 和幂等 key。
2. 固定本次请求使用的 Analyzer、ranking config 和 model profile JSON 版本。
3. 调用任务画像模型，根据 `task_query` 和 `request_context` 生成规范化 `task_profile`。
4. 若任务画像模型安全失败，使用服务内确定性 fallback profile。
5. 从 model profile JSON 中读取候选模型。
6. 只保留 `status` 为 `enabled` 或 `canary` 且角色、能力和上下文满足要求的模型。
7. 使用 `task_profile`、`user_profile`、成本、延迟和角色可靠性进行评分。
8. 选择主 proposers 和一个主 aggregator。
9. 保存内部决策记录，返回两字段响应。

服务根据 `task_query` 和 `request_context` 估算输入 token；候选和 aggregator 输出预算使用 ranking config 的固定默认值。V1 不接收附件、完整 conversation、workspace、tool log 或 intermediate outputs。

## 5. OpenSquilla 执行流程

1. 从当前用户配置中生成 `user_profile`。
2. 从当前会话生成可选的 `request_context` 摘要。
3. 调用 `model-router`，只发送 `task_query`、`request_context` 和 `user_profile`。
4. 接收主 proposers 和主 aggregator。
5. 将模型标识解析为本地 provider/deployment。
6. 按本地配置设置 thinking、quorum、超时、重试和 recovery。
7. 使用现有 `EnsembleProvider` 完成 proposer fan-out 和 aggregator 融合。
8. 记录 usage、费用和执行 trace；只有融合成功后才更新 session last route。

V1 不返回排名生成的 backup roster，因此只保证首次主 P/A 选择，不保证与当前跨模型 recovery 顺序完全一致。

## 6. 服务配置

服务内部维护：

- Task Analyzer prompt、输出 Schema、模型和 fallback policy。
- `router_dynamic_ranking_config.json`。
- `router_dynamic_model_profiles.json`。

配置要求：

- 每次请求固定同一版本配置，处理中不热切换。
- model profile JSON 包含模型 status、角色、能力、价格、延迟和 `role_reliability`。
- 只有 `status` 为 `enabled` 或 `canary` 的模型可以进入排序。
- `role_reliability` 继续由审计通过的实验产物离线更新，不根据单次线上请求直接改写。
- 配置发布前必须验证 Analyzer 输出 Schema 与 ranking core 接受的 task-profile Schema 一致。

## 7. 代码拆分

迁入 `model-router`：

- `build_request_context` 中与 `task_query` 和有界 `request_context` 相关的最小构造逻辑。
- Task Analyzer prompt、Schema、fallback chain 和 provider adapter。
- `provider/ranking_router.py` 中的 task profile 校验、registry、硬过滤、评分和主 P/A 选择。
- ranking config 和 model profile JSON。

保留在 OpenSquilla：

- 用户画像来源和 session 生命周期。
- `provider/ensemble.py` 的 member 物化、调用、quorum 和 recovery。
- `engine/runtime.py` 的 turn、工具和 conversation 生命周期。
- `engine/routing/health.py` 的执行期健康记录。
- DRACO run/resume/finalizer 的冻结实验、replay 和最终审计。

建议最小服务结构：

```text
opensquilla-model-router/
  src/model_router/
    api.py
    contracts.py
    analyzer.py
    registry.py
    ranking.py
    store.py
```

OpenSquilla 只新增 routing client；provider/deployment 解析继续复用现有实现。

## 8. V1 非目标

- 不接收或返回 `task_profile`。
- 不接收 `model_profile` 或运行时候选列表。
- 不返回 decision ID、backup proposers 或 aggregator fallback candidates。
- 不迁移 task-specific thinking assignment。
- 不新增 reroute、outcome、health 或 evidence 数据面接口。
- 不远程执行 DRACO replay/finalizer。
- 不删除本地完整动态路由路径；V1 验证稳定后再评估后续迁移。

## 9. 迁移步骤

1. 从现有代码拆出 Task Analyzer 和首次 P/A ranking core。
2. 定义只含 `task_query`、可选 `request_context`、`user_profile` 的请求 Schema和两字段响应 Schema。
3. 在服务内加载 ranking config 与 model profile JSON。
4. 使用冻结任务和固定 Analyzer 结果做离线 golden 测试，避免 shadow 重复付费调用 Analyzer。
5. 小流量启用服务；失败时回退本地动态路由或单模型路径。
6. V1 稳定后，再独立评估结构化 context、attachments、backup、thinking 或 recovery 扩展。

## 10. 验收标准

- 请求体严格只有 `task_query`、可选 `request_context` 和 `user_profile`。
- 成功响应严格只有 `proposers` 和 `aggregator`。
- 服务只调用任务画像模型，proposer/aggregator 物理调用数恒为 0。
- model profile 只从服务内部版本化 JSON 加载。
- 返回模型全部存在于当前 model profile，且 status 为 `enabled` 或 `canary`。
- Analyzer 输出始终通过 task-profile Schema 校验，失败时只使用确定性 fallback profile。
- Golden corpus 中首次 selected P/A 与约定的本地基线一致。
- 当前 ranking、runner/resume 和 DRACO finalizer 测试不回归。
- 普通日志和错误响应不包含 `task_query`、`request_context`、`user_profile`、模型凭证或 API key。

## 11. 现有实现锚点

- `provider/ranking_router.py:3742`：request context 构造。
- `provider/ranking_router.py:4902`：Task Analyzer。
- `provider/ranking_router.py:7116`：上下文预算可行性。
- `provider/ranking_router.py:7167`：status 和可用性过滤。
- `provider/ranking_router.py:7369`：角色可靠性扣分。
- `provider/ranking_router.py:8465`：ranking core 入口。
- `provider/ensemble.py:20906`：当前路由与执行混合入口。
- `provider/ensemble.py:22176`：RankingDecision 到执行成员的物化。
- `engine/runtime.py:7188`：当前 Analyzer 后的 ranking inputs。
