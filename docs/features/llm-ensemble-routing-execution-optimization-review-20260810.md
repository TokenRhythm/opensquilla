# 多模型路由与融合执行优化 Review

日期：2026-08-10

审查分支：`feature/multi-llm-ensemble-routing2`

审查基线：`618b0ee51c33e64c5a1079d9aaf6406b8ec4a298`

审查范围：最近 85 个相关提交（2026-07-22 至 2026-08-10）、动态模型路由、Task Analyzer、proposer/aggregator ensemble、恢复与降级、usage/费用取证、DRACO run/resume/finalizer。

> 本文只给出审查结论和改动规划。本次没有修改任何运行代码、配置或实验数据。

## 1. 结论摘要

目前的实现已经具备较强的执行与审计基础：proposer 与 aggregator 有分角色可靠性、物理调用有 attempt/usage 证据、未关闭流可隔离、执行成功与费用审计已解耦、DRACO 支持 Analyzer 多模型 fallback 和固定融合兜底、聚合恢复有预算上限。

当前最影响效率和稳定性的，不是排序公式本身，而是运行编排上的五个问题：

1. **动态 ensemble 默认可能等待最慢 proposer**：`quorum_grace_seconds=0` 的实际语义是继续等待全部 pending proposer，proposer/aggregator 默认超时又是 3600 秒，尾延迟会被慢模型主导。
2. **生产 Task Analyzer 仍是单模型重复尝试**：线上 runtime 仍对 Opus 4.8 最多执行 4 次、每次 20 秒；DRACO 已实现的 Opus → GPT-5.6-sol → Gemini 链尚未进入通用 runtime。
3. **成员模型的实时失败没有回灌动态路由**：已有 `ProviderHealthLedger`，但 ensemble 成员调用没有完整接入；故障模型可能在下一回合再次被选中。
4. **缺少跨 turn、跨 provider 的并发背压**：单个 turn 最多并发 5 个 proposer，之后还有 proposer recovery 和 Top-3 aggregator；多个会话并发时容易放大 429、排队和清理压力。
5. **缺少贯穿 Analyzer、proposer、recovery、aggregator 的绝对 deadline**：各阶段各自拥有超时，恢复时还可能重新获得完整预算，外层取消容易留下未关闭流和 unknown usage。

建议优先完成“统一 deadline + quorum 后有界等待 + 实时健康闭环 + 全局并发控制 + 通用 Analyzer fallback”。这五项能直接改善 p95/p99、429、重复失败和未关闭流，且不需要放宽当前审计边界。

## 2. 最近相关提交回顾

| 主题 | 代表提交 | 已解决的问题 | 仍需补齐 |
| --- | --- | --- | --- |
| 模型池与可靠性 | `618b0ee5`、`1e740771`、`a3c2363c`、`3597d154`、`04f6730d` | status 过滤、角色可靠性惩罚、length-capped proposer 计失败、usage 归因强化 | 样本覆盖低、冷启动零罚分、缺 upstream/时间衰减/canary 流量控制 |
| Analyzer 与路由降级 | `0589155e`、`4234004d`、`6670c135`、`0b943129` | DRACO 三段 Analyzer 链、链耗尽固定 4P+1A、冻结回放与证据校验 | 通用 runtime 未接入；每候选独立超时，无全链总预算 |
| Ensemble 恢复 | `e34ee696`、`662a2358`、`c9755943`、`33ce5e4f`、`3e86317e`、`46a1bc7a` | retry metadata/provider 状态隔离、工具恢复保护、部分输出恢复 | 动态模式仍等慢 proposer；recovery 串行导致尾延迟 |
| Stream 与费用证据 | `9c18bee2`、`36bc016b`、`7e9e38d6`、`7007599d` | 未关闭流隔离、physical attempt 绑定、执行与审计解耦、认证残缺流 | receipt 状态机分散在 provider/ensemble/runner/finalizer 多处 |
| DRACO 恢复与审计 | `bb7197ea`、`1152f220`、`6c5d815c`、`708438be`、`9bc61c60` | frozen policy、跨 wave 重试控制、不可变快照和 finalizer 门禁 | run/resume 大量复制；异常收尾、持久化和大文件扫描仍可优化 |
| Prompt cache | `800078c6` | TokenRhythm/qwen3.7-max 显式 cache-control 与 usage 记录 | 无 system prompt 时 TokenRhythm marker 不会生效；缺角色级命中率指标 |

总体判断：最近提交主要补强了“失败后如何安全恢复、如何把费用说清楚”，方向正确。下一阶段应从继续叠加局部 guard，转向统一执行预算、健康状态和公共编排模块。

## 3. 当前设计中应保留的边界

后续优化不应破坏以下已经验证有效的原则：

- `execution_status` 与 `audit_status` 分开；可用答案不因费用证据不完整而消失。
- 未关闭流、物理 attempt 冲突、工具副作用等仍保留严格隔离和取证。
- 普通动态 ensemble 仍使用较强 quorum；“1 个完整 proposer 即可聚合”仅适用于已认证的 Analyzer 全失败固定兜底。
- partial/length-capped proposer 不冒充 complete proposer。
- aggregator 恢复保持有界，不因失败无限 continuation 或切换模型。
- 费用优先采用 provider 实际金额；缺金额时才按 cache-aware token 估算，并与账户实际支出分列。
- 历史 registry/config/hash 回放合同保持兼容。
- thinking 自动档位开关继续默认关闭，除非实验显式开启。
- DRACO 质量实验可以显式选择 `wait_for_all`；线上默认策略不应反向改变实验口径。

## 4. 优先级改进项

### P0-1：统一绝对 deadline，避免各阶段重复获得完整超时

**现状**

- router_dynamic proposer/aggregator 默认 timeout 都是 3600 秒。
- `_collect_candidate_inner()` 会用 ensemble proposer timeout 覆盖调用方 `ChatConfig.timeout`。
- proposer recovery 串行执行，每个附加请求又可能获得完整 timeout。
- `ensemble_soft_deadline_seconds` 主要在 Agent wrap-up 路径使用，普通调用缺少完整 phase budget。

**规划**

- 在一次 turn/ensemble 开始时生成唯一 `absolute_deadline`。
- 明确划分 Analyzer、proposer、recovery、aggregator reserve；所有请求使用：

  `effective_timeout = min(role_cap, caller_timeout, remaining_deadline)`

- retry/backoff 不重置 deadline；若剩余时间不足以完成最小 aggregator reserve，就停止追加 proposer。
- 为 interactive、normal、batch/experiment 提供不同 latency class；DRACO 继续使用显式实验参数。

**验收**

- `ChatConfig.timeout=50ms`、ensemble proposer timeout=3600s 时，总调用不会突破调用方 deadline。
- 任意 retry 后 remaining budget 单调下降。
- 超时退出后无 pending task、未跟踪 stream 或新增物理请求。
- trace 记录 configured、effective、remaining 和触发 deadline 的阶段。

### P0-2：Quorum 后有界等待，不让最慢 proposer 主导线上尾延迟

**现状**

- `_run_proposers()` 会同时启动全部 selected proposer。
- `quorum_grace_seconds=0` 只有在所有 proposer 都完成后才进入聚合；该行为已有契约测试固化。
- 因此 quorum 早已满足时，慢/挂死 proposer 仍可能阻塞整个融合。

**规划**

- 给 router_dynamic 增加显式的 quorum grace，建议先以 2–10 秒做实验，而不是直接固定某个值。
- quorum 达成后只等待 grace；之后取消 pending、完成有界 stream close，并保存 unknown usage/隔离证据。
- 保留 `wait_for_all=true` 作为质量优先和 DRACO 实验选项。
- 第二阶段再评估 progressive fanout：先发质量/多样性核心 proposer，超过 hedge delay 或失败时再发剩余候选。

**验收**

- 1 快 + 1 慢/挂死模型时，quorum 后在 grace 内进入 aggregator。
- 取消后无残留 asyncio task、无重复 cleanup、物理请求数不越预算。
- 对照实验同时报告质量、p50/p95/p99、selected generation 成本和 degraded rate。

### P0-3：把 DRACO Analyzer fallback 变成通用 runtime 能力

**现状**

- 通用 runtime 仍调用 `analyze_task_with_provider()`，对单一 Opus 4.8 最多重复 4 次，每次 20 秒。
- `analyze_task_with_fallback_chain()` 已实现一候选一请求、usage 累计和 cleanup/physical evidence 保护，但目前主要由 DRACO runner 使用。
- 中文输入使用 `ensure_ascii=True`，Unicode 转义会放大 prompt 字节/token；当前只在序列化前按字符截断 message，没有约束完整 Analyzer JSON。

**规划**

- 在公共 ranking config/runtime 中支持有序 Analyzer chain：Opus 4.8 → GPT-5.6-sol → Gemini 3.1 Pro。
- 使用一个全链绝对 deadline，不给每个候选独立完整 20 秒。
- auth/unsupported 等不可重试错误直接切下一候选；429/5xx 使用受限退避；schema 修复最多一次并携带结构化错误码。
- 三路都失败后使用确定性本地 profile 或显式固定 ensemble 兜底。
- JSON 使用 `ensure_ascii=False`，并对完整 payload 施加 token/byte 总预算。

**验收**

- 不可重试错误只产生 1 个物理请求。
- 任意三路故障都在全链 deadline 内结束。
- production 与 eval 生成同结构 chain trace；usage、attempt id 和 cleanup 可审计。
- 中文长输入不因 Unicode 转义突破 payload/token 上限。

### P0-4：把 ensemble 成员实时健康接回动态 hard filter

**现状**

- `ProviderHealthLedger` 已支持 strike、cooldown、429 Retry-After 和 never-strand。
- 动态排序的 hard filter 主要读取模型画像中的静态 health/quota/rate-limit；画像时间较旧，latency 仍是 curated estimate。
- ensemble 成员失败目前主要更新 credential cooldown，没有完整写入共享 deployment health ledger。

**规划**

- 以 `(provider, model, upstream/deployment)` 为键记录成功、timeout、429、5xx、auth、cleanup timeout。
- 在构建 runtime registry facts 时注入 benched/half-open、Retry-After、近窗成功率和 EWMA p95。
- hard filter 跳过已 bench 候选，但保留 never-strand；cooldown 后只允许少量 half-open 探测。
- 首版使用进程内共享 ledger；多进程共享状态作为后续扩展。

**验收**

- 某成员首回合连续失败达到阈值后，下一回合不再入选。
- cooldown 后仅发受控 half-open 请求；成功恢复、失败重新 bench。
- 429 不产生同 upstream 的并发重试风暴。

### P0-5：增加跨 turn/provider 的并发背压

**现状**

- 每个 turn 可并发启动最多 5 个 proposer，之后还可能有 3 次 proposer recovery 和 Top-3 aggregator fallback。
- 多会话之间没有共享 provider/deployment semaphore；并发度会随会话数线性放大。

**规划**

- 引入按 provider/deployment 加权 semaphore，并增加全局 in-flight 上限。
- proposer、recovery、Analyzer、aggregator 都进入同一 admission budget。
- 队列等待受 absolute deadline 约束；超时返回可解释的 admission failure，不在队列中无限等待。
- 对昂贵/低并发 endpoint 使用更低权重；429 Retry-After 同健康 ledger 联动。

**验收**

- 50 个并发 turn 下，真实 in-flight 不超过配置。
- 无 semaphore 泄漏或饥饿；queue wait、drop、timeout 都可观测。
- 429 率和 p99 相比基线下降。

### P0/P1-6：修复三处高确定性正确性问题

#### 6.1 零费用 canonical 字段被 legacy 别名覆盖

`_with_model_usage_cost_fields()` 能保留 confirmed zero，但 `_summarize_model_usage_breakdown()` 使用 `row.get("billed_cost") or row.get("billedCost")`。当 canonical snake_case 为 `0`、legacy camelCase 为正值时，汇总可能错误恢复成正成本。

规划：所有 usage/cost 字段统一采用“键存在优先”，禁止 truthiness fallback；规范化后删除冲突 alias。补 confirmed-zero + mixed-alias 回归测试。

#### 6.2 continuation 去重会误删短重合文本

`_deduplicate_continuation()` 对任意 `overlap > 0` 都裁剪。若上一段以 `a` 结尾、续写以 `answer` 开头，可能被裁成 `nswer`。

规划：只接受完整 prefix，或达到最小长度且位于词/行/token 边界的 overlap；1–2 字符和中文单字重合默认不裁剪。补英文、中文、代码 token 和长重合测试。

#### 6.3 runner worker 异常没有统一 cancel/await/final manifest

主 runner/resume 一次性创建任务后用 `as_completed()` 收集；任一 worker 抛异常时，没有统一 supervisor 保证 cancel+await 其他任务，也未必把 manifest 从 running 原子改为 aborted。

规划：改为 `TaskGroup` 或有界 worker supervisor；普通 worker 异常封装为 sealed failure row，fatal 异常则 cancel+await 全部，`finally` 原子发布 terminal manifest。

**验收**

- 三类问题分别有最小复现和回归测试。
- 一个 worker 抛错、另一个延迟或抗取消时，最终无活任务、无新增请求；manifest 为 terminal 且记录 rows/failures。

## 5. P1 效率与架构优化

### P1-1：减少 selection plan/trace 的体积与深拷贝

当前每次决策会把完整 registry snapshot、ranking config、request_context、candidate pool 和全量 scores 放进 trace；执行、事件和 retry 上下文又多次 `deepcopy`。模型画像文件约 259 KB，单 turn trace 可达到数百 KB。

规划：

- trace 默认只保存 config/registry 的 hash、version、选中项和过滤/评分摘要。
- 完整 config/registry 作为 content-addressed artifact 存一次。
- 全量 debug trace 仅在采样或显式诊断时开启。
- 不可变 selection plan 以只读结构共享，避免事件/重试重复复制。

验收：普通 selection plan 序列化小于 32 KB，或较基线下降至少 80%；100 并发 turn 的 RSS/event-loop lag 有稳定上界；hash 加载后 replay 结果字节等价。

### P1-2：优化 proposer 正文缓冲与聚合 prompt

当前 proposer delta 同时追加完整 `text_parts`，又反复执行 `result.text + delta` 后截断；`candidate_max_chars` 没有真正约束内部缓冲，极端流式输出会产生额外内存和近似二次字符串复制。Aggregator 又会接收多个最长 24K 字符的候选。

规划：

- 使用 bounded chunk buffer；达到 cap 后不再保存正文，仅累计 total chars/hash/truncated flag/usage。
- 对候选做确定性的去重和压缩，不新增一次 LLM 调用：保留引用、工具证据、数字、结论和 content hash，删除重复段落。
- 保持 aggregator prompt 的稳定 cacheable prefix，把易变候选和工具上下文放在 cache breakpoint 之后。

验收：100K 个单字符 delta 的峰值内存为 `O(cap)`；最终正文/hash/usage 正确；aggregator input tokens 和成本下降，质量不显著退化。

### P1-3：Deadline-aware proposer recovery

当前 `_recover_proposers_serially()` 会逐失败槽、逐 backup/thinking/transient await，成本受控但尾延迟高。

规划：

- 正常情况下保持串行，避免无谓成本。
- 当 quorum deficit 大于 1 且剩余 deadline 较短时，最多并行 2 个 recovery。
- 并发宽度受 quorum deficit、provider semaphore、物理调用预算和 remaining deadline 共同约束。
- 达到 quorum 立即停止追加并安全关闭剩余流。

验收：两个独立 1 秒 recovery 的 deadline 紧张场景，墙钟接近 1 秒而非 2 秒；总物理调用不越预算；普通场景不增加调用。

### P1-4：可靠性统计、角色状态和 canary 真正闭环

当前 59 个 enabled 模型中，仅少量模型有 role reliability 观测；`prior_success=10, prior_failure=0` 使 0 观测模型得到零惩罚，可能优于任何出现过一次失败的已观测模型。`status=canary` 又与 `enabled` 完全同等进入排序。

规划：

- 用层级 Beta 先验或可信区间上界表达冷启动不确定性；0 样本不再等价于“完美稳定”。
- 增加每角色最小样本和 coverage 门槛；数据不足时降低 reliability 权重而不是制造强排序差异。
- 把可靠性至少细分为 proposer/aggregator + upstream；样本足够后再按 tool/no-tool 或任务类别分层。
- 支持角色级 eligibility，例如模型仅禁用 proposer、保留 aggregator。
- canary 使用稳定 hash 采样、低风险任务、流量上限、健康门槛和自动回滚，不再默认全量可选。
- 完成审计的实验/生产证据可自动生成候选 snapshot，但必须经内容 hash、来源 manifest 和 review 后发布；不做静默在线改写。

验收：0 观测模型不系统性优于高置信成功模型；留出集任务成功率、校准和成本不退化；canary 真实流量不超过配置。

### P1-5：抽取 DRACO run/resume 公共核心

当前主 runner 约 16.7K 行、resume 约 22.7K 行；两者共享 269 个顶层函数，其中 264 个 AST 相同。最近多次出现 main/resume 只修一侧的风险。

规划分步抽取：

1. `draco_runtime_contract.py`：配置、route pin、registry/hash 合同。
2. `draco_analyzer_chain.py`：Analyzer usage、trace、fallback 激活。
3. `draco_recovery.py`：provider-owned recovery、retry budget、lifecycle。
4. `draco_usage_evidence.py`：physical receipt、unknown/estimated cost。
5. `draco_artifact_index.py`：wave/result/manifest 索引。

run/resume 只保留启动方式、checkpoint 与 repair 适配。迁移期间保留 AST/source parity tests，并用历史 manifest golden tests 保证兼容。

### P1-6：DRACO durable write 与 finalizer 单遍索引

当前 JSONL 写入主要依赖 flush，manifest 直接 `write_text`；进程崩溃可能丢失已付费行或留下截断 manifest。Finalizer 对大 result shard 重复 hash、parse、验证，产生多次全文件扫描。

规划：

- result/trace 按行或小批 `fsync`；manifest 采用 temp + fsync + atomic replace + directory fsync。
- 写入进度 checkpoint，记录 sealed row、attempt 和 source offset。
- 首遍流式读取时同时计算 SHA、解析记录并构建 task/attempt/judge 索引。
- 后续 gate 复用索引；最后仅做必要的 TOCTOU hash/stat 复核。
- 大归档使用 offsets/SQLite，避免把全部行长期保存在内存。

验收：kill -9 后恢复不重复付费行；manifest 永不出现半写 JSON；500 MB/1 GB artifact finalization 的 I/O pass、耗时和 RSS 明显下降。

### P1-7：密钥热轮换与 Retry-After 贯通

当前 key pool fingerprint 主要基于 env 名列表；同一 env 名的 secret 值轮换后，进程可能继续使用旧缓存 key。Pool 能处理 `retry_after_seconds`，但 ensemble 成员失败上报未完整贯通该 hint，常退化成固定 cooldown。

规划：

- fingerprint 加入 secret 的不可逆 digest，或使用短 TTL 重新解析；变化后清理旧 pin/cooldown，绝不记录明文。
- ErrorEvent → candidate → credential reporter 贯通 Retry-After，并施加合理上下界和 jitter。

验收：同名 env 从 key A 更新为 B 后，下一次 acquire 使用 B；日志只出现 hash；429 cooldown 与 provider hint 一致。

## 6. P2 优化与可维护性

### P2-1：修复 TokenRhythm 无 system prompt 的显式缓存

`explicit_cache_supported` 当前只在存在 system prompt 时计算，因此 qwen3.7-max 在 `cache_mode=on`、但无 system prompt 时不会给 user message 添加 marker。

规划：把 capability 判断移出 system 分支；与 DashScope 一样覆盖“无 system 但有 user”的测试。同步记录 proposer/aggregator 的 cache read/write tokens 和命中率。

### P2-2：预编译不可变 routing snapshot

每个 turn 会重复 legacy projection、严格校验、全量 hash、模型模板线性查找。候选规模只有约 59，排序本身不是瓶颈，但重复构建和深拷贝会浪费 CPU。

规划：启动/配置变更时编译 immutable registry index、policy、hash 和模型 map；turn 内只 overlay credential、health、permission 等 runtime facts。相同 hash 缓存命中，配置变更精准失效。

### P2-3：统一 PhysicalAttemptReceipt 状态机

将 started/streaming/done/error/close-timeout/usage-known/usage-estimated/quarantined 规范化为 provider adapter 负责的 receipt 状态机。Ensemble、runner、resume 和 finalizer 只消费同一 schema，避免四处推断同一物理请求。

这项应在现有行为完全有测试保护后实施，不能借重构放宽 tool lifecycle、cleanup 或费用证据门禁。

### P2-4：补齐执行指标和 SLO

建议增加：

- Analyzer latency、selected index、chain exhausted。
- snapshot build、filter、score、trace serialization 的耗时与字节数。
- provider admission queue、in-flight、bench/half-open、429/5xx。
- proposer time-to-quorum、quorum-to-cancel、cleanup lag、recovery calls。
- aggregator continuation/fallback、task success/degraded/failed。
- proposer/aggregator 分角色 token、actual/estimated cost、cache hit、unknown usage。

任何 metrics、trace 和日志都不得包含 API key、完整用户正文或隐藏 reasoning。

## 7. 建议实施顺序

### 阶段 0：建立基线，不改变行为

- 加入分阶段 latency、in-flight、trace bytes、time-to-quorum、cleanup lag、429 和 unknown usage 指标。
- 固化当前线上/DRACO 的质量、成本、p50/p95/p99、失败与降级率基线。
- 增加 fault matrix 和性能测试，避免优化时只能依赖单次 DRACO 分数。

### 阶段 1：低风险正确性与 deadline

- 修零费用 alias、continuation 去重、runner 异常收尾。
- 引入全链 absolute deadline，但先保持现有 fanout 与 quorum 口径。
- 修 bounded proposer buffer、TokenRhythm 无 system cache。

### 阶段 2：线上稳定性闭环

- 通用 Analyzer fallback chain。
- ensemble member → health ledger → hard filter。
- provider/deployment 并发背压。
- router_dynamic quorum grace；先 shadow，再小流量 canary。

### 阶段 3：效率策略实验

- progressive proposer fanout。
- deadline-aware recovery concurrency。
- deterministic candidate compaction 与 aggregator cache layout。

这些策略必须通过成对实验比较质量、成本、延迟和 degraded rate，不建议一次全部默认开启。

### 阶段 4：架构收束

- 抽取 DRACO run/resume 公共核心。
- 建立 artifact index 和单遍 finalizer。
- 统一 PhysicalAttemptReceipt。
- 升级可靠性 snapshot 与 canary 发布流程。

## 8. 测试与验收矩阵

| 类别 | 必测故障 | 核心断言 |
| --- | --- | --- |
| Analyzer | timeout、429、5xx、auth、invalid JSON、cleanup timeout | 全链 deadline；不可重试错误不重放；usage/attempt 完整 |
| Proposer | 慢响应、length、partial visible、半关闭流、DNS | quorum/grace 正确；complete-only 不误收 partial；无 task 泄漏 |
| Aggregator | length、无 Done、有/无工具生命周期、fallback 失败 | 恢复最多一次或配置上限；工具副作用不重复；可用前缀可降级交付 |
| Admission | 多 turn 同 provider、Retry-After、半开探测 | in-flight 上限、无饥饿、429 不放大 |
| Usage/费用 | actual=0、alias 冲突、usage missing、cache read/write、BYOK | canonical 0 不被覆盖；估算 cache-aware；账户与理论费用分列 |
| Durable | kill -9、磁盘短写、manifest 中断 | 不重复已付费请求；JSON 原子；resume 可继续 |
| Finalizer | 500 MB/1 GB shards、文件变更 | 单遍索引；TOCTOU 检出；RSS/耗时上界 |
| Soak | 1000 turns、随机断流/取消 | pending cleanup、task、内存和 fd 不增长 |

每项行为变更至少同时验证：

- 物理请求数上限。
- 端到端 deadline。
- 无泄漏/无重复副作用。
- execution/audit 状态没有重新耦合。
- 历史 frozen replay 与 manifest hash 兼容。
- DRACO 质量、selected generation 成本和有效完成率不显著退化。

## 9. 推荐首批改动清单

首批控制在可独立 review 的小改动，不进行大重构：

1. 修复 usage/cost canonical-zero alias 优先级。
2. 修复 continuation 短 overlap 误裁剪。
3. 给普通 ensemble 贯通 caller absolute deadline。
4. 给 runner worker 增加统一 supervisor 和 terminal manifest。
5. 修复 TokenRhythm 无 system prompt cache marker。
6. 增加 time-to-quorum、trace bytes、in-flight、cleanup lag 指标。

第二批再做通用 Analyzer chain、健康 ledger、并发背压和 quorum grace。这样可以先消除确定性 bug，再改变执行策略，便于定位质量或成本波动。

## 10. 非目标

- 不通过降低 Judge 标准提升表面成功率。
- 不把 partial proposer 全局当成 complete proposer。
- 不因 BYOK 或费用缺失直接判任务执行失败，但仍需单独标记 audit 状态。
- 不在运行时静默修改冻结模型画像。
- 不为了追求低延迟取消所有质量冗余；质量优先实验仍可 wait-all。
- 不把隐藏 reasoning 写入 conversation、trace 或聚合 prompt。

## 11. 最终建议

若只选三件最值得立即做的事：

1. **统一 deadline，并在 quorum 后有界退出**，直接解决慢模型拖垮整条任务的问题。
2. **把实时成员健康和 provider 并发背压接入动态路由**，避免跨回合重复选择坏模型并抑制 429 风暴。
3. **让生产 runtime 复用已验证的 Analyzer fallback chain**，消除 Opus 单点和同模型原样重试。

这三项会比继续微调 ranking 权重带来更直接的效率和稳定性收益。完成后，再推进 staged fanout、可靠性统计升级和 DRACO 架构收束。
