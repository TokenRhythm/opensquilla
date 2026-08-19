# 多模型路由独立服务设计（最小版）

状态：Implemented（部署启用状态单独管理）

实现基线：

- OpenSquilla 分支 `feature/multi-llm-ensemble-routing2`，commit `797987b705d8e484e9c7975f56964631fd8752b4`。
- 经评审的 Python 源码树共 948 个文件，SHA-256 为 `5e5bf150fbf70384a24c02b9972f8f56d2fce4d7639afef0006eaabb3542c57e`。
- vendored wheel `opensquilla-0.5.0-py3-none-any.whl` 的 SHA-256 为 `329df29aeafff963447e18d2b5109e7bc00e70f0e53a8928726d06cb85e4d731`。
- ranking config 为 `step2-ranking-2026-08-18.4`；模型画像 snapshot 为 `curated-openrouter-step2-2026-08-19.1-reliability-20260818T121632Z-01580c6982b4`。

## 1. 设计结论

`model-router` 根据任务选择 proposer 和 aggregator。

- 请求只传 `task_query`、可选的结构化 `request_context` 和 `user_profile`。
- 服务内部调用任务画像模型，生成 `task_profile`。
- `model_profile` 不通过 API 传递，由服务维护版本化 JSON 配置。
- 成功响应只返回 `proposers` 和 `aggregator`。
- thinking、quorum、重试、recovery、Canary、工具调用和模型融合留在 OpenSquilla。
- 候选分数、过滤原因、task profile、配置版本和哈希只保存在服务内部。

V1 只负责首次选择主 proposer 和主 aggregator，不返回 backup roster、aggregator fallback chain 或 task-specific thinking assignment。

## 2. 职责边界

| `model-router` | OpenSquilla |
| --- | --- |
| 校验 `task_query`、`request_context` 和 `user_profile` | 从当前 turn/session 生成受限的结构化任务上下文和用户画像 |
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

请求头 `Idempotency-Key` 必填，格式为 1–128 个 `[A-Za-z0-9._:-]` 字符。`/v1` normalization 固定标识为 `model-router-v1`，其缺省值、集合排序、canonical JSON 和哈希语义在 `/v1` 生命周期内不可变；语义变更必须发布新的 API 版本。已绑定 key 的同一 canonical body 重放已保存的 `200`/`422` 终态，不同或无法 canonicalize 的 body 返回 `409`；记录引用的 normalizer 不可用时返回 `503`，不得用新规则解释或重新执行。

请求：

```json
{
  "task_query": "结合刚才的评审结果，给出最终实现方案",
  "request_context": {
    "summary": "用户正在设计多模型路由服务；上一轮已确定接口应保持最小化，并要求依据现有代码完善 request_context。",
    "last_route": {
      "selected_P": [
        "openrouter:openai/gpt-5.6-sol",
        "openrouter:google/gemini-3.1-pro-preview"
      ],
      "selected_A": "openrouter:z-ai/glm-5.2",
      "quality_feedback": 0.8,
      "escalation_level": 0
    },
    "routing_budget": {
      "estimated_input_tokens": 6400,
      "tool_log_tokens": 400,
      "candidate_output_tokens": 24000,
      "aggregator_output_tokens": 8192
    },
    "input_modalities": ["text"]
  },
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

- `task_query` 必须是非空字符串，V1 最多 24000 个 Unicode code points。
- `request_context` 是可选对象，用于描述当前 query 出现时的有界上下文；缺省为 `{}`。
- `user_profile` 必须传入且必须是对象；没有个性化信息时传空对象 `{}`。
- 请求中不传 `task_profile`、`model_profile`、ranking config、候选模型、模型凭证或运行时健康账本。
- 完整 HTTP request body 上限为 1,048,576 bytes；`Content-Length` 和无 `Content-Length` 的流式请求执行同一上限。未绑定 key 时超限返回 `400`；已绑定 current-V1 key 时，因为无法证明 canonical equality，返回 `409`。

`request_context` 由 OpenSquilla 从当前 turn/session 生成。当前 `task_query` 不在其中重复；对话、工具、workspace、中间结果和附件的必要语义都先压缩进 `summary`，不传完整 conversation、原始工具日志、文件内容或完整 workspace 状态。

除 `task_query` 和 `user_profile` 外，`request_context` 及其所有子字段都可省略。最小合法值是 `{}`；普通的上下文依赖请求通常只需发送 `summary`。

#### 3.1.1 `request_context` 结构

公共 API 不直接暴露当前 `build_request_context()` 的全部内部字段，只保留首次 P/A 选择真正需要的最小投影：

| 字段 | 类型与缺省 | 用途 |
| --- | --- | --- |
| `summary` | `string`，`""`，最多 4000 字符 | 理解当前 query 必需的会话、工具、workspace 和中间状态摘要；映射到内部 `conversation.summary` |
| `last_route` | `object`，`{}` | 上一次路由，用于 continue/redo、模型黏性和失败后的 tier 升级 |
| `routing_budget` | `object`，服务计算缺省值 | 本次真实输入、工具日志和 P/A 输出预算，用于 context-window 硬过滤 |
| `input_modalities` | `string[]`，`["text"]` | 当前请求实际需要的原生输入模态，用于 modality 硬过滤 |

完整 V1 Schema：

- 所有对象都设置 `additionalProperties: false`；未知字段或子字段类型错误返回 `400`，不静默忽略。
- `summary` 必须是字符串，最多 4000 字符；超限返回 `400`，不由服务隐式截断。
- `last_route.selected_P` 是最多 8 个、不重复、非空的模型标识字符串；`selected_A` 是非空模型标识字符串；每个标识最多 512 个 Unicode code points。两者均省略时，整个 `last_route` 规范化为 `{}`。
- `last_route.quality_feedback` 是有限数字，范围 `[0, 1]`，缺省 `0.5`；`escalation_level` 是整数，范围 `[0, 2]`，缺省 `0`。布尔值不作为数字或整数接受。
- `routing_budget.estimated_input_tokens` 和 `tool_log_tokens` 是 `[0, 10000000]` 内的整数；`candidate_output_tokens` 和 `aggregator_output_tokens` 是 `[1, 1000000]` 内的整数。布尔值不作为整数接受。
- partial `routing_budget` 的缺省逐项为：`estimated_input_tokens` 使用服务估算且至少为 `1`，`tool_log_tokens=0`，`candidate_output_tokens=24000`，`aggregator_output_tokens=8192`。
- `input_modalities` 只有两个合法 canonical 值：`["text"]` 或 `["text", "image"]`；缺省为 `["text"]`。OpenSquilla 根据真实输入生成该投影，服务不接收附件正文，也不根据文件名猜测模态。
- `request_context` 省略、传 `null` 或传 `{}` 都规范化为同一个空上下文。

服务把该最小投影转换为当前 ranking core 使用的内部 context：

```json
{
  "conversation": {
    "summary": "<request_context.summary>",
    "recent_turns": []
  },
  "tool_state": {
    "called_tools": [],
    "tool_results_summary": "",
    "failed_tools": []
  },
  "workspace_state": {
    "referenced_files": [],
    "changed_files": [],
    "test_results": "unknown"
  },
  "intermediate_outputs": {
    "previous_candidates": [],
    "current_errors": []
  },
  "last_route": {
    "selected_P": [
      "openrouter:openai/gpt-5.6-sol",
      "openrouter:google/gemini-3.1-pro-preview"
    ],
    "selected_A": "openrouter:z-ai/glm-5.2",
    "quality_feedback": 0.8,
    "escalation_level": 0
  },
  "routing_budget": {
    "estimated_input_tokens": 6400,
    "tool_log_tokens": 400,
    "candidate_output_tokens": 24000,
    "aggregator_output_tokens": 8192
  },
  "input_modalities": ["text"],
  "attachment_refs": [],
  "snapshot_hash": "<service generated>"
}
```

预算规范化规则：服务按当前代码的白名单投影估算输入 token，即 `task_query + conversation + workspace_state + intermediate_outputs + last_route + input_modalities + attachment_refs`；不把 `tool_state`、`routing_budget` 或 `snapshot_hash` 再算进输入，避免双计。最终 `estimated_input_tokens` 取服务估算和客户端值的较大者。OpenSquilla 的真实执行 prompt 若还包含该投影之外的 material，必须传本次完整输入估算；实际工具上下文非空时，必须传真实 `tool_log_tokens`，否则缺省为 `0`。省略输出预算时，V1 固定使用 `candidate_output_tokens=24000`、`aggregator_output_tokens=8192`；若本次执行预算不同，必须传实际值。API 上限、缺省、集合排序、canonical JSON 与哈希规则属于不可变的 `/v1` Schema；ranking config 更新不得改变同一个 V1 请求的 canonical body。

幂等处理先按 `Idempotency-Key` 查询已保存记录。首次请求原子保存 canonical request hash、固定配置版本和终态响应；当前二进制只接受持久化的 `model-router-v1`，任何未知 normalization version 都返回 `503` 并 fail closed。相同 canonical body 返回原响应，不同或无法规范化的 body 返回 `409`。

这四个字段都有实际路由语义：`summary` 影响任务画像；`last_route` 影响会话连续性得分与 redo 升级；`routing_budget` 影响 context-window 硬过滤；`input_modalities` 影响 modality 硬过滤。当前内部的 recent turns、tool/workspace/intermediate 和 attachment refs 不成为公共 API 字段，其必要语义由 OpenSquilla 汇总进 `summary`。

#### 3.1.2 `user_profile` 规范化

`user_profile` 只允许当前画像合同中的 `permission`、`preference` 和 `history` 三个对象，子字段沿用稳定、版本化的 V1 user-profile Schema；所有层级未知字段均返回 `400`。`{}` 表示启用中性画像：无模型/risk 权限限制，`quality_latency_tradeoff=balanced`、`cost_sensitivity=medium`，且没有正负模型历史；`null` 不合法。服务先应用这些确定性缺省，再计算 canonical request hash。

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
| 400 | `INVALID_REQUEST` / `INVALID_IDEMPOTENCY_KEY`：未绑定 key 的 media type、JSON、Schema、1 MiB body limit 或 key 格式无效 |
| 409 | `IDEMPOTENCY_CONFLICT`：已绑定 current-V1 key 的 body 不同或无法 canonicalize |
| 422 | `NO_FEASIBLE_ROUTE`：没有满足要求的 proposer 或 aggregator；这是会被重放的确定性终态 |
| 429 | `ANALYZER_OVERLOADED`：物理调用前安全拒绝，带 `Retry-After`，不消费 key |
| 503 | `ANALYZER_UNAVAILABLE`、`ANALYZER_STATE_UNCERTAIN`、`IDEMPOTENCY_STATE_UNCERTAIN`、`IDEMPOTENCY_NORMALIZATION_UNAVAILABLE` 或 `SERVICE_UNAVAILABLE` |
| 504 | `IDEMPOTENCY_WAIT_TIMEOUT`：只结束调用方等待，后台 single-flight 继续，带 `Retry-After`，不启动第二次 Analyzer |

错误响应只返回简短的 `code` 和 `message`，部分可重试状态另带 `Retry-After` header。OpenSquilla 按现有本地策略决定回退，不把回退计划放进路由响应。

## 4. 服务内部流程

1. 在进程内按 key 建立覆盖 reserve、admission、Analyzer 和 ranking 全流程的 single-flight；同 key/同 body 的并发调用共享同一个后台任务。
2. 查询 SQLite 幂等记录；命中后按不可变的 `model-router-v1` 比较 body，直接返回原终态、`409` 或 fail-closed `503`。
3. 对首次请求校验 `task_query`、结构化 `request_context` 和 `user_profile`，原子 reserve，并固定 Analyzer、ranking config 和 model profile JSON 版本。
4. 用稳定的 V1 adapter 将最小 `request_context` 转成内部完整 context，并在 Analyzer 前计算 `snapshot_hash`。
5. 在任何物理调用前把 `side_effect_started` 持久化；随后调用任务画像模型，根据 `task_query` 和内部 context 生成 `task_profile`。若 Analyzer payload 超过 48000 字符、96000 bytes 或 32000 estimated tokens，沿用当前逻辑先把 context 压成 `routing_budget + input_modalities + last_route + hashes`，再有界截断 task。
6. 若任务画像模型可证明安全地失败，使用服务内确定性 fallback profile；若物理调用/stream cleanup 状态无法证明，则将该 key 标为 `unknown` 并返回 fail-closed `503`。
7. 使用同一内部 context 从 model profile JSON 中读取候选模型，只保留 `status` 为 `enabled` 或 `canary` 且角色、能力和上下文满足要求的模型。
8. 使用 `task_profile`、`request_context`、`user_profile`、成本、延迟和角色可靠性评分，选择主 proposers 和一个主 aggregator。
9. 先用 SQLite WAL、`synchronous=FULL` 提交 `200` 或确定性 `422` 终态，再返回响应。服务不持久化 query、context、user profile、Analyzer payload/output 或完整 ranking trace。

进程重启时，`side_effect_started=0` 的 unpaid `in_progress` reservation 会被释放；已开始或可能已开始物理调用的 reservation 变为 `unknown`，同 key 不自动重试。SIGTERM 先停止 readiness/接单并限时 drain；未完成任务按相同规则收敛状态后再取消。

V1 不自动清理 terminal 或 `unknown` 记录。删除记录会允许同一幂等 key 再次触发付费 Analyzer，因此部署侧必须监控 SQLite/WAL 容量；若将来引入有限保留期，必须作为显式 API 合同变更发布，`unknown` 记录不得自动清理。

服务按上述白名单投影估算输入 token，并结合请求给出的本次输出预算完成 context-window 可行性检查。V1 只接收一个有界 `summary`，不接收附件内容/引用、recent turns、原始 workspace、原始 tool log 或完整 intermediate outputs。

## 5. OpenSquilla 执行流程

1. 从当前用户配置中生成 `user_profile`。
2. 从当前 turn/session 生成可选的结构化 `request_context`；只投影白名单字段并遵守稳定的 V1 Schema 边界。
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
- V1 不新增上游 tier 字段；调用现有 Analyzer/ranking core 时固定使用兼容默认 `routed_tier="c1"`、`routing_confidence=0.0`。本地 V1 golden adapter 使用相同值。
- model profile JSON 包含模型 status、角色、能力、价格、延迟和 `role_reliability`。
- 只有 `status` 为 `enabled` 或 `canary` 的模型可以进入排序。
- `role_reliability` 继续由审计通过的实验产物离线更新，不根据单次线上请求直接改写。
- 配置发布前必须验证 Analyzer 输出 Schema 与 ranking core 接受的 task-profile Schema 一致。

### 6.1 Analyzer transport

任务画像调用使用服务专用 `RedactedOpenRouterProvider`，不复用会输出 request/response 内容的通用 provider logger 或 trace recorder：

- 只接受官方 `https://openrouter.ai/api/v1`，禁止重定向并设置 `trust_env=False`。
- 每个 Analyzer route 严格 pin 一个 upstream provider，禁止 OpenRouter 自行 fallback；Opus 4.8 → GPT-5.6-sol → Gemini 3.1 Pro Preview 的模型间 fallback 由外层冻结 chain 控制。
- 禁止 response cache，provider response 上限为 1,000,000 bytes。
- 日志和错误响应只记录无内容的错误分类，不记录 upstream body、异常正文、prompt、payload 或模型输出。

### 6.2 Packaging 与部署

- 从评审 commit `797987b...` 构建 vendored OpenSquilla Python wheel；安装时验证 wheel SHA，启动时再验证完整 948-file Python tree hash。运行时不依赖相邻 editable checkout。
- ranking config 与模型画像 JSON 是服务包内资源；`uv.lock` 固定完整依赖解析。
- systemd 固定 `User=codex`、`127.0.0.1:8092`、一个 Uvicorn worker；`StateDirectory=agentic-routing-api` 以 mode `0700` 提供 `/var/lib/agentic-routing-api`。
- SQLite DB 使用独占 flock，禁止第二个进程共享；多 worker/多副本前必须迁移到共享事务型幂等存储。

## 7. 代码拆分

`model-router` 实现：

- 新增最小 V1 context adapter，把 `summary`、`last_route`、`routing_budget` 和 `input_modalities` 映射成 ranking core 的内部 context；复用现有预算估算、snapshot hash 和 Analyzer payload 压缩逻辑。OpenSquilla 保留本地完整 `build_request_context` 供 fallback/DRACO 使用。
- Task Analyzer prompt、Schema、fallback chain 和无内容日志的专用 provider adapter。
- 通过经哈希校验的 reviewed wheel 复用 `provider/ranking_router.py` 中的 task-profile 校验、registry、硬过滤、评分和主 P/A 选择；服务只提供 thin facade，不维护第二份排序算法。
- 服务包内 ranking config 和 model profile JSON。

保留在 OpenSquilla：

- 用户画像来源和 session 生命周期。
- `provider/ensemble.py` 的 member 物化、调用、quorum 和 recovery。
- `engine/runtime.py` 的 turn、工具和 conversation 生命周期。
- `engine/routing/health.py` 的执行期健康记录。
- DRACO run/resume/finalizer 的冻结实验、replay 和最终审计。

实际最小服务结构：

```text
agentic-routing-api/
  vendor/
  src/agentic_routing_api/
    app.py
    contracts.py
    context.py
    service.py
    analyzer.py
    openrouter_transport.py
    ranking.py
    config_bundle.py
    idempotency.py
    core_compatibility.py
    settings.py
    resources/
  deploy/
  scripts/
  tests/
```

OpenSquilla 只新增 routing client；provider/deployment 解析继续复用现有实现。

## 8. V1 非目标

- 不接收或返回 `task_profile`。
- 不接收 `model_profile` 或运行时候选列表。
- 不返回 decision ID、backup proposers 或 aggregator fallback candidates。
- 不迁移 task-specific thinking assignment。
- 不新增 reroute、outcome、health 或 evidence 数据面接口。
- 不把 recent turns、tool/workspace/intermediate 明细、附件引用或 `required_parameters_by_role` 暴露为 V1 API 字段；必要语义先汇总进 `summary`。
- 不支持按请求覆盖上游 `routed_tier` 或 `routing_confidence`；V1 使用服务内固定兼容默认。
- 不远程执行 DRACO replay/finalizer。
- 不删除本地完整动态路由路径；V1 验证稳定后再评估后续迁移。

## 9. 迁移步骤

1. 从 `797987b...` 构建并校验 vendored minimal Python wheel，冻结两份 JSON 资源。
2. 实现只含 `task_query`、可选结构化 `request_context`、`user_profile` 的请求 Schema 和两字段响应 Schema。
3. 实现最小 V1 context adapter、专用 Analyzer transport、Task Analyzer chain、primary P/A facade 与幂等状态机。
4. 使用相同 V1 context adapter、冻结任务和固定 Analyzer 结果做离线 exact golden 测试，避免把本地 full-context 与服务 minimal-context 的输入差异误报成 ranking 差异，也避免 shadow 重复付费调用 Analyzer。
5. 通过制品、systemd 和无真实模型调用的 smoke gate 后，才允许小流量启用；失败时由 OpenSquilla 按本地策略处理。
6. V1 稳定后，再独立评估附件正文、完整执行态、backup、thinking 或 recovery 扩展。

## 10. 验收标准

- 请求体严格只有 `task_query`、可选结构化 `request_context` 和 `user_profile`。
- 成功响应严格只有 `proposers` 和 `aggregator`。
- 服务只调用任务画像模型，proposer/aggregator 物理调用数恒为 0。
- model profile 只从服务内部版本化 JSON 加载。
- 返回模型全部存在于当前 model profile，且 status 为 `enabled` 或 `canary`。
- Analyzer 输出始终通过 task-profile Schema 校验；可证明安全的失败使用确定性 fallback profile，无法证明 physical/cleanup 状态的失败必须标记 unknown 并 fail closed。
- `request_context` 的省略/空值等价、字段白名单、边界裁剪、token 预算下限、模态和 last-route 行为都有契约测试。
- 契约测试必须证明未绑定 key 的超限、重复、越界、布尔伪装数字和未知字段返回 `400`；已绑定 current-V1 key 的无效/不同 body 返回 `409`，不得复用内部 sanitizer 做静默截断、去重或 clamp。
- 1 MiB body limit 对 `Content-Length` 与 chunked/streamed body 都生效，超限请求不调用 Analyzer。
- `model-router-v1` normalization 不可变，未知持久化 version 返回 `503` 且不重跑。
- 同 key 高并发只执行一次 reserve/admission/Analyzer；一个 waiter 断线或 `504` 不取消后台任务、不增加 Analyzer 调用数。
- admission `429` 不持久化为终态，所有同 key follower 看到一致结果；槽位恢复后可用同 key安全重试。
- unpaid/paid restart 分别执行 release/unknown；reserve cancellation、terminal commit failure、SIGTERM 和 SQLite 锁/磁盘错误均 fail closed。
- 第二个服务进程被 DB flock 拒绝；WAL、Schema version 和 DB 可写性不满足时 readiness 失败。
- 专用 Analyzer transport 只允许 official endpoint/strict upstream，且任何 HTTP/transport/oversize 错误都不会把 payload、response body、异常正文或 API key 写入日志。
- Golden corpus 中首次 selected P/A 与使用同一个 V1 context adapter 的本地基线一致。
- vendored wheel checksum、完整 Python tree hash、wheel install/resource smoke、systemd unit verify 均通过。
- 当前 ranking、runner/resume 和 DRACO finalizer 测试不回归。
- 普通日志和错误响应不包含 `task_query`、`request_context`、`user_profile`、模型凭证或 API key。

## 11. 现有实现锚点

以下行号均对应 OpenSquilla `797987b705d8e484e9c7975f56964631fd8752b4`：

- `provider/ranking_router.py:4286`：request context 构造。
- `provider/ranking_router.py:5317`：单个 Task Analyzer 调用。
- `provider/ranking_router.py:6262`：Task Analyzer fallback chain。
- `provider/ranking_router.py:7591` / `:7785`：上下文预算需求与可行性应用。
- `provider/ranking_router.py:7619`：status 和可用性过滤。
- `provider/ranking_router.py:7821`：角色可靠性扣分。
- `provider/ranking_router.py:10214`：ranking core 入口。
- `provider/ensemble.py:22440`：当前路由与执行混合入口；实际 rank call 在 `:23687`。
- `provider/ensemble.py:24046`：RankingDecision 到执行成员的物化。
- `engine/runtime.py:9623` / `:9714` / `:9760`：context、Analyzer 与 ranking inputs。
