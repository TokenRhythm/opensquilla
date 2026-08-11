# Ensemble execution metrics dashboard 与 SLO 契约（v1）

状态：v1 producer 已实现；有默认关闭的本地结构化 JSONL transport；dashboard
与 SLO 仍是建议契约，仓库尚无 collector、指标后端、datasource、ruler 或
dashboard provisioning。

证据基线：`69cb3088` 中的 `src/opensquilla/observability/ensemble_execution_metrics.py` 及 `tests/test_observability/test_ensemble_execution_metrics.py`。

事件名：`llm_ensemble.execution.metrics`。
Schema：`opensquilla.ensemble-execution-metrics/v1`。

JSONL transport schema：`opensquilla.ensemble-execution-metrics-jsonl/v1`。

## 1. 范围与边界

本契约只消费 v1 producer 已经投影出的固定枚举、布尔值和有界数值，不读取或转发原始 ensemble trace。producer 对一次 Agent→provider 事件流的首个 terminal event 最多记录一条 metrics event；`terminal_outcome` 只有 `completed` 和 `failed`，先出现的 terminal event 决定结果。日志投影和日志后端均为 fail-open，失败不会影响模型调用，也不会重试本次 metrics event。

因此，本文的比率只描述“成功进入指标后端的 v1 events”，不是全部 ensemble 调用的绝对可用性。当前没有独立 invocation counter 或 delivery acknowledgement，无法量化 metrics event 丢失率。

主要解决：先固定可被现有证据支持的观测边界，避免把日志缺失误判为业务成功。

### 1.1 可选本地 JSONL transport

POSIX gateway 可以显式设置
`OPENSQUILLA_ENSEMBLE_METRICS_JSONL=1`，将 projector 已生成的同一份固定
scalar metrics 写入本地 JSONL。默认目录是
`$OPENSQUILLA_LOG_DIR/ensemble-metrics/`；没有设置共享日志目录时使用
`$OPENSQUILLA_STATE_DIR/logs/ensemble-metrics/`。也可以用
`OPENSQUILLA_ENSEMBLE_METRICS_JSONL_DIR` 指定绝对目录。目标的父目录必须由
当前 uid 所有且不可被 group/world 写入；专用目录固定 `0700`，data、backup
和 lock files 固定 `0600`。路径按组件拒绝 symlink，regular-file fd、路径
identity、owner、mode 和 hard-link count 会在锁内复验。

每行是扁平 JSON，包含 `transport_schema`、固定 `event`、UTC
`emitted_at` value 和本页字段字典允许的 metrics。`emitted_at` 只用于采集时间，
不得转成 label。sink 再次执行固定字段 allowlist、类型、枚举和数值上界校验；
它只接收 projector result，不读取原始 trace。未知字段、嵌套 object/list、
NaN/Inf、identity/hash、原始 selection plan/candidates/final request 都使该 sink
fail closed，但不影响普通 structlog event 或模型 turn。

单行硬上限为 65,536 bytes。data file 在 5,000,000 bytes 前轮转，最多保留
3 个 backup，即正常稳态最多约 20 MB 加一行；同进程由 thread lock 串行，
跨进程以专用 owner-only lock fd 和 `flock` 包住 size check、rename rotation 与
单次 `os.write`。多线程进程 `fork` 时，child 不获取可能由消失线程持有的锁；
它按 inode identity 直接关闭继承的 directory、lock 和 active data fd，下一次
写入再 lazy reopen，避免已轮转或删除的 data inode 被 child 长期占用。它是
best-effort transport，不 `fsync`、不重试，也没有 delivery acknowledgement；
轮转后的本地文件不能证明 metrics delivery coverage。

这项 transport **不是 dashboard backend**：仓库仍没有 collector、
Prometheus/OpenTelemetry exporter、Loki/时序数据库、Grafana datasource、
alert ruler、通知目标或可加载 dashboard。因此不能把 JSONL 文件本身称为
“已部署 dashboard/SLO”，也不能新增孤立 dashboard JSON/rules YAML。后续选择
并接入真实 backend 时，collector 只能消费通过 `transport_schema`、`event` 和
`schema` 三重过滤且通过同一 allowlist 的 rows。

router-dynamic snapshot build、hard filter、score latency 与本次 packaged-template
lookup 的 cache hit 已有独立 producer evidence 和固定 projector；metrics delivery
coverage 仍保持 unavailable。本 transport 只承运已通过 contract 的投影行，不能
反向证明投影行已被 collector 摄取。Same-host persistent canary rollback 已有独立
receipt projection，仍不代表 multi-host coordination、quality rollback 或整体请求
latency evidence。

主要解决：为后续真实采集器提供可解析、低基数、空间有界的本地交接面，同时
明确它没有越级完成 dashboard、告警或缺失 producer 的观测能力。

## 2. 证据语义

| 术语 | v1 含义 | Dashboard / SLO 使用规则 |
| --- | --- | --- |
| `*_observed=true` | 对应来源字段存在、类型合法，或 producer 已证明该聚合可用；不同字段的完整性条件见字段字典。 | 只有观察标志为真且目标值存在时，目标值才进入计算。 |
| `*_observed=false` | 没有足够来源证据；不是数值 0，也不是失败。 | 不进入 numerator，也不进入 denominator；单独进入 evidence coverage 面板。 |
| `*_projection_complete=true` | producer 已扫描完整相关集合，逐行证据一致，且未触发 candidate/usage cap；role admission 还要求一对一 attempt join。 | 无后缀 role totals 可作为 exact totals；完整性为假时不得回填 0。 |
| 无后缀 exact value | 该字段本身满足 producer 的精确输出条件。例如完整 proposer usage totals、完整 role admission totals、完整 aggregator physical request total。 | 可进入 exact SLI；仍需检查相邻 `observed` / `projection_complete` / `scan_capped` gate。 |
| `*_lower_bound` | 只证明“至少这么多”。v1 当前用于 capped trace size 和不完整 role admission counts/waits。 | 只用于容量与证据缺口面板；禁止与 exact totals 混合，禁止作为成功率 denominator。 |
| `*_scan_capped=true` | producer 只扫描固定前缀：candidate 64、Analyzer attempts 8、aggregator attempts 16、每个 candidate usage rows 8。 | 前缀 counts 只能视为 lower-bound/diagnostic；总量型 SLO 必须排除 capped events。 |
| 字段缺失 | 字段无合法 producer 证据，或受 overflow/malformed/cap gate 抑制。 | 缺失不是 0；聚合器必须保留 null/unavailable。 |
| unavailable | v1 没有对应 producer 字段或无法证明 denominator。 | 不创建伪指标、不从相邻字段推断；面板显示 unavailable 和所缺证据。 |

`observed` 不总等于“看见任意一行”。例如 `proposer_physical_request_count_observed=true` 要求所有 candidates 的 dispatch evidence 完整；`aggregator_physical_request_count_observed=true` 要求 aggregator stage 真实发生、attempt 未 capped 且每个 attempt 都有 physical count。`aggregator_final_request_usage_observed` 与其 `projection_complete` 同义，只覆盖最后一个 aggregator request。

主要解决：统一 exact、lower bound、observed 与 projection complete 的消费方式，防止缺失证据污染分母。

## 3. 固定低基数字段字典

所有枚举都必须映射到下表列出的固定 domain；未知输入由 producer 映射为 `unknown` 或不输出。所有数值只能作为 value，不能作为 label。花括号表示有限枚举展开，不表示任意动态字段。

### 3.1 Core、outcome 与 trace

| 字段 | Domain / 输出条件 | 语义 |
| --- | --- | --- |
| `schema` | 固定 `opensquilla.ensemble-execution-metrics/v1` | 解析版本。 |
| `terminal_outcome` | `completed`、`failed` | 首个 terminal event 类型。 |
| `execution_status` | `success`、`degraded`、`failed` | `failed` 优先；completed event 在 fallback、明确 degradation、partial quorum 或 usable length cap 时为 degraded。 |
| `selection_family` | `router_dynamic`、`router_tree_baseline`、`fixed`、`unknown` | selection strategy 的低基数归类。 |
| `fallback_used_observed`、`fallback_used` | bool；value 只在 source bool 存在时输出 | 是否有明确 fallback evidence。 |
| `trace_size_observed` | bool | trace 是否可按严格 JSON 标量/容器规则测量。 |
| `trace_compact_json_bytes_capped` | bool；仅 size observed 时 | 是否在 byte、visit 或 depth cap 停止。 |
| `trace_compact_json_bytes` | exact；未 capped | compact UTF-8 JSON bytes。 |
| `trace_compact_json_bytes_lower_bound` | lower bound；capped | 已证明的最小 bytes。 |
| `trace_compact_json_bytes_cap`、`trace_compact_json_visit_cap` | 固定 262144、16384 | 测量工作上界。 |
| `trace_compact_json_bytes_cap_reason` | `byte_limit`、`visit_limit`、`depth_limit` | capped 原因。 |

router-dynamic 的 terminal trace 现在可带顶层
`ranking_stage_observability`，schema 固定为
`opensquilla.router-dynamic-ranking-stage-observability/v1`。它来自私有 sidecar，
不进入 `selection_plan`、ranking decision、selection fingerprint 或 replay 输入；
因此 nondeterministic 计时不会改变选择或历史重放字节。

| 字段 | 输出条件与精度 | 语义 |
| --- | --- | --- |
| `ranking_stage_observed` | 顶层 block 为 built-in dict 且 schema 精确匹配 | 识别到版本化阶段证据；不表示其中所有值都合法。 |
| `ranking_snapshot_build_ms_observed`、`ranking_snapshot_build_ms` | schema 匹配，value 为非 bool、0..2^63-1 的整数毫秒 | 构造本 turn registry snapshot 的 monotonic elapsed time。0 是合法真实观测。 |
| `ranking_hard_filter_ms_observed`、`ranking_hard_filter_ms` | 同上 | proposer 与 aggregator 两段 hard-filter monotonic elapsed time 之和。 |
| `ranking_score_ms_observed`、`ranking_score_ms` | 同上 | proposer score/selection 阶段 monotonic elapsed time。 |
| `ranking_stage_projection_complete` | 固定字段集合无未知键，三个计时都合法；可选 cache 字段若存在也必须为 strict bool | 三段 latency 可共同消费的总 gate；cache 字段不是 complete 的必需项。 |
| `ranking_packaged_template_cache_hit_observed`、`ranking_packaged_template_cache_hit` | 默认 packaged registry lookup 原子返回 strict bool 时 | 本次 lookup 是否命中 process-local immutable template index。显式或历史 snapshot 不输出该字段。 |

cache primitive 在同一次 lookup 内原子返回 index 与 hit/miss；projector 不读取
`cache_info()`，也不以进程 counter delta 猜测请求归属。缺少 cache 字段不是
`false`，必须从 cache-hit denominator 排除。三个计时均来自真实函数边界的
monotonic clock，并被规范化为非负有界整数；未知 schema、坏类型、越界值或
未知字段只会降低对应 observed/complete gate，不会阻断模型调用。

仍然 unavailable：end-to-end ensemble latency、metrics delivery coverage、trace
schema revision hash、snapshot version/hash，以及显式/历史 snapshot 的 cache-hit
语义。snapshot version/hash、model/provider identity、cache key 与 raw sidecar
永远不能成为 metrics value 或 label。

主要解决：给三个真实排名阶段和默认 packaged-template cache 建立请求级证据，
同时保持 deterministic ranking/replay 与低基数隐私边界。

### 3.2 Task Analyzer

| 字段族 | 输出条件与精度 | 语义 |
| --- | --- | --- |
| `task_analyzer_observed` | `selection_plan.task_analyzer` 为 dict | Analyzer block 存在。 |
| `task_analyzer_source_observed`、`task_analyzer_source_family` | source 合法；family 为 `live_provider`、`frozen_replay`、`fallback`、`local`、`unknown` | Analyzer 来源家族，不含 provider/model identity。 |
| `task_analyzer_schema_valid_observed`、`task_analyzer_schema_valid` | source bool 存在 | Analyzer output schema 是否有效。 |
| `task_analyzer_chain_observed`、`task_analyzer_chain_attempts_observed` | chain / attempt list 存在 | fallback chain evidence gate。 |
| `task_analyzer_chain_attempt_count`、`task_analyzer_chain_attempt_scan_count`、`task_analyzer_chain_attempt_scan_capped` | attempt list 存在 | 总行数、最多 8 行的扫描数及 cap。 |
| `task_analyzer_chain_success_count`、`task_analyzer_chain_failed_count` | scanned prefix | prefix outcome counts；capped 时是 lower bound。 |
| `task_analyzer_chain_physical_request_observation_count`、`task_analyzer_chain_physical_request_count` | scanned rows 中合法 counts | count 是已观察前缀和；只有未 capped 且 observation count 等于 scan count 时可当 exact。 |
| `task_analyzer_selected_field_observed`、`task_analyzer_selected`、`task_analyzer_selected_index` | selected_index 为 null 或合法非负整数 | 是否选中以及固定数值 index；index 不能作 label。 |
| `task_analyzer_exhausted_observed`、`task_analyzer_exhausted` | bool 存在 | chain 是否耗尽。 |
| `task_analyzer_deadline_observed` | deadline dict 存在 | deadline block gate。 |
| `task_analyzer_deadline_configured_ms`、`task_analyzer_elapsed_ms`、`task_analyzer_deadline_remaining_ms` | 非负有限 seconds 可换算 | Analyzer deadline/elapsed scalar。 |
| `task_analyzer_deadline_expired_observed`、`task_analyzer_deadline_expired` | bool 存在 | deadline 是否已过期。 |

Analyzer 的 token、usage missing、billed cost、cache、logical-terminal HTTP status 和逐 attempt latency 当前 unavailable。

主要解决：让 Analyzer fallback、schema、deadline 与 physical attempts 可观测，同时避免从 chain prefix 推断完整总量。

### 3.3 Proposer candidates、health 与 logical terminal

| 字段族 | 输出条件与精度 | 语义 |
| --- | --- | --- |
| `proposer_candidates_observed` | candidates 为 list | candidate block gate。 |
| `proposer_candidate_count`、`proposer_candidate_scan_count`、`proposer_candidate_scan_capped` | candidates observed | 总行数、最多 64 行扫描数及 cap。 |
| `proposer_elapsed_observation_count`、`proposer_candidate_elapsed_ms_max`、`proposer_candidate_elapsed_ms_total` | started 且 elapsed_ms 为合法整数 | observed prefix latency；不是完整 per-request histogram。 |
| `proposer_runtime_health_observed`、`proposer_runtime_health_observation_count` | candidate health block 存在 | runtime health admission evidence。 |
| `proposer_runtime_health_tracked_observation_count`、`proposer_runtime_health_tracked_count` | tracked bool | health registry tracking。 |
| `proposer_runtime_health_state_observation_count` | state token 存在 | health state denominator。 |
| `proposer_runtime_health_{healthy,benched,half_open}_count`、`proposer_runtime_health_unknown_state_count` | scanned health rows | 固定状态 counts。 |
| `proposer_runtime_health_probe_observation_count`、`proposer_runtime_health_probe_count` | probe bool | half-open probe 标志；不证明 probe 成功。 |
| `proposer_runtime_health_{benched,half_open_busy,unknown}_deferred_count` | `request_started=false` 且 physical count 为 0 | 真正 pre-dispatch deferred counts。 |
| `proposer_logical_terminal_http_status_observed`、`proposer_logical_terminal_http_status_observation_count` | started、physical count >0 且 error code 是 100–599 | 可解析 logical terminal status denominator。 |
| `proposer_logical_terminal_rate_limited_count`、`proposer_logical_terminal_upstream_5xx_count` | 上述 status rows | 429 和 5xx counts。 |

HTTP 成功状态、无 error-code physical requests、provider/model/deployment identity 均未投影；因此 v1 只能计算“已观察 terminal status 的构成”，不能计算全 physical request 的真实 429/5xx error rate。

主要解决：区分 health pre-dispatch defer 与真实上游调用失败，避免把未调用模型的情况计为 429/5xx。

### 3.4 Runtime health filter、half-open 与 never-strand

| 字段族 | 输出条件与精度 | 语义 |
| --- | --- | --- |
| `runtime_health_filter_observed` | selection plan 中 filter block 为 dict | filter evidence gate。 |
| `runtime_health_filter_enabled_observed`、`runtime_health_filter_enabled` | bool 存在 | filter 是否启用。 |
| `runtime_health_requires_rerank_observed`、`runtime_health_requires_rerank` | bool 存在 | health 变化是否要求 rerank。 |
| `runtime_health_input_candidate_count`、`runtime_health_fresh_deployment_count` | 合法非负整数 | filter 输入和 fresh deployment 数。 |
| `runtime_health_{proposer,aggregator}_active_unavailable_count` | role count 存在 | active unavailable 数。 |
| `runtime_health_{proposer,aggregator}_filtered_count` | role count 存在 | 被 hard filter 的数。 |
| `runtime_health_{proposer,aggregator}_half_open_count` | role count 存在 | half-open 数。 |
| `runtime_health_{proposer,aggregator}_never_strand_minimum` | role count 存在 | never-strand 最小候选数。 |
| `runtime_health_{proposer,aggregator}_never_strand_exempt_count` | role exemption list 存在 | 仅记录列表长度，不记录 identity。 |
| `runtime_health_never_strand_observed`、`runtime_health_never_strand` | bool 存在 | 本次 selection plan 的 never-strand 标志。 |

当前没有 half-open probe 成功率、deployment 级状态、never-strand violation、被豁免 identity 或 health transition latency；这些全部 unavailable。

主要解决：监控动态 hard filter 是否频繁压缩候选池，同时不泄露 deployment/model identity。

### 3.5 Admission

| 字段族 | 输出条件与精度 | 语义 |
| --- | --- | --- |
| `admission_observation_count`、`admission_wait_observation_count` | 从 top-level、candidate 和 final request 可见 rows 汇总 | 可见 admission rows；可能无法一对一 join，仅用于诊断。 |
| `admission_timeout_count`、`admission_rejected_count` | 可见 rows | 跨 role 可见 counts；不是 exact attempt denominator。 |
| `admission_wait_ms_max`、`admission_wait_ms_total` | 可见合法 waits | 跨 role observed totals；不是 role exact SLO。 |
| `{proposer,aggregator}_admission_observed` | 对应 role row 或 typed proposer admission error 可见 | role evidence gate。 |
| `{proposer,aggregator}_admission_projection_complete` | candidate/attempt 一对一 join、未 capped 且 dispatch evidence 一致 | exact role gate。 |
| `{role}_admission_observation_count`、`{role}_admission_wait_observation_count` | role rows 可见 | observed rows / waits。 |
| `{role}_admission_{admitted,timeout,rejected}_count` | projection complete | exact counts。 |
| `{role}_admission_{admitted,timeout,rejected}_count_lower_bound` | observed 但 projection incomplete | lower bounds。 |
| `{role}_admission_wait_ms_{max,total}` | projection complete 且每行 wait 有效 | exact waits。 |
| `{role}_admission_wait_ms_{max,total}_lower_bound` | projection 或 wait coverage 不完整 | lower-bound waits。 |

当前没有 global/provider/deployment admission tier、queue capacity、deadline budget、stable attempt id 或 admission wait histogram；这些全部 unavailable。

主要解决：只在一对一 attempt 证据完整时计算 admission SLO，并把不可 join 的可见行保留为 lower bound。

### 3.6 Quorum、cleanup 与 proposer recovery

| 字段族 | 输出条件与精度 | 语义 |
| --- | --- | --- |
| `quorum_observed`、`quorum_reached_observed`、`quorum_reached` | quorum block / bool 存在 | quorum gate 与结果。 |
| `time_to_quorum_ms`、`quorum_grace_elapsed_ms`、`pending_at_quorum` | 合法非负整数 | quorum latency、grace 和 pending。 |
| `cleanup_observed` | cleanup block 非空 | cleanup gate。 |
| `quorum_cancel_requested_task_count` | cancellation count 合法 | 请求取消任务数。 |
| `cleanup_awaited_task_count`、`cleanup_completed_task_count`、`cleanup_lingering_task_count` | 合法 counts | worker cleanup 结果。 |
| `cleanup_stream_close_proven_count`、`cleanup_stream_close_unproven_count` | 合法 counts | stream close 证明。 |
| `proposer_recovery_observed`、`proposer_recovery_calls` | additional physical requests count 合法 | proposer recovery 额外 physical calls。 |

当前没有 cleanup latency、每个 recovery outcome、recovery billed cost 或取消原因；这些全部 unavailable。

主要解决：发现 quorum 后的残留 task/stream 和额外 recovery 成本风险，避免只看最终文本成功。

### 3.7 Aggregator stage 与 logical terminal

| 字段族 | 输出条件与精度 | 语义 |
| --- | --- | --- |
| `aggregator_recovery_observed`、`aggregator_recovery_attempts_observed` | recovery block / list 存在 | recovery 容器 gate。 |
| `aggregator_stage_observed` | 至少一个有证据 attempt 或合法 selected kind | 真实 aggregator stage denominator；预初始化空 block 不进入。 |
| `aggregator_recovery_attempt_count`、`aggregator_recovery_attempt_scan_count`、`aggregator_recovery_attempt_scan_capped` | attempt list 存在 | 总行数、最多 16 行扫描数及 cap。 |
| `aggregator_{primary,continuation,same_model_recovery,model_fallback,continuation_fallback}_attempt_count`、`aggregator_unknown_kind_attempt_count` | scanned prefix | kind counts。 |
| `aggregator_request_started_observation_count`、`aggregator_request_started_count` | request_started bool | dispatch evidence。 |
| `aggregator_physical_request_observation_count`、`aggregator_physical_request_count_observed`、`aggregator_physical_request_count` | 每个 attempt count 完整且未 capped | exact physical total 仅在 observed=true 时输出。 |
| `aggregator_{succeeded,failed,abandoned,unavailable,unknown_outcome}_attempt_count` | scanned prefix | 互斥 outcome buckets；五项之和等于 scan count。 |
| `aggregator_unsuccessful_attempt_count` | failed + abandoned | 已执行且未成功；不含真正 pre-dispatch unavailable。 |
| `aggregator_runtime_health_observed`、`aggregator_runtime_health_{deferred,benched_deferred,half_open_busy_deferred,unknown_deferred}_count` | pre-dispatch unavailable evidence 一致 | aggregator health defer。 |
| `aggregator_logical_terminal_http_status_observed`、`aggregator_logical_terminal_http_status_observation_count` | started、physical count >0 且 code 是 HTTP status | logical terminal status denominator。 |
| `aggregator_logical_terminal_rate_limited_count`、`aggregator_logical_terminal_upstream_5xx_count` | 上述 status rows | 429 和 5xx counts。 |
| `aggregator_selected_kind_observed`、`aggregator_selected_kind` | 固定 `primary`、`continuation`、`same_model_recovery`、`model_fallback`、`continuation_fallback`、`partial_salvage`、`degraded_delivery`、`unknown` | 最终选择家族。 |
| `aggregator_fallback_index_observed`、`aggregator_fallback_index` | 合法非负整数 | 数值 index，不能作 label。 |
| `aggregator_recovery_{success,exhausted,degraded}_observed`、对应 value | stage observed 且 bool 存在 | recovery terminal state。 |
| `aggregator_{continuation,same_model_recovery}_count_observed`、对应 value | stage observed 且 count 合法 | recovery 次数。 |

当前没有 aggregator attempt elapsed、TTFT 或全请求 HTTP status denominator；这些全部 unavailable。所有已启动 aggregator attempts 的 usage/cost/cache 证据见 3.8。

主要解决：把 aggregator failure、abandon、pre-dispatch unavailable 和 unknown outcome 分开，防止失败被 recovery 容器掩盖。

### 3.8 分角色 usage、cost 与 cache

| 字段族 | 输出条件与精度 | 语义 |
| --- | --- | --- |
| `proposer_physical_request_count_observation_count`、`proposer_physical_request_count_observed`、`proposer_physical_request_count` | 所有 candidate dispatch evidence 完整且未 capped | proposer exact physical total。 |
| `proposer_unknown_usage_count_observation_count`、`proposer_unknown_usage_count_observed`、`proposer_unknown_usage_count` | 所有 candidates 有一致 missing count | proposer unknown usage total。 |
| `proposer_usage_observed`、`proposer_usage_projection_complete` | usage containers 可见；完整性还要求每个 physical request 对应一行、无 unknown/malformed/cap | role usage gate。 |
| `proposer_usage_container_observation_count`、`proposer_usage_row_scan_count`、`proposer_usage_row_scan_capped` | scanned usage rows | 容器与 prefix scan。 |
| `proposer_usage_receipt_count`、`proposer_usage_unknown_row_count`、`proposer_usage_malformed_row_count` | scanned prefix | known、unknown、malformed rows；不完整时只作诊断。 |
| `proposer_usage_row_count_observed`、`proposer_usage_row_count` | projection complete 且至少一个 container | exact receipt-row denominator。 |
| `proposer_{input,output,reasoning,cache_read,cache_write}_tokens_observation_count` | 对应字段为合法整数的 known rows | field coverage。 |
| `proposer_{input,output,reasoning,cache_read,cache_write}_tokens` | projection complete 且每个 known row 有该字段 | exact role totals。 |
| `proposer_cache_hit_request_count` | projection complete 且所有 cached token fields 完整 | cached_tokens >0 的 exact request count。 |
| `proposer_billed_cost_usd_observation_count`、`proposer_billed_cost_usd` | projection complete 且每个 known row 有合法 billed_cost | observed billed total；未验证 cost source/exactness。 |
| `aggregator_usage_observed`、`aggregator_usage_attempt_observation_count`、`aggregator_usage_started_attempt_count` | attempt usage v1 block 可见；started count 还要求 request_started 完整且未 capped | 所有 aggregator attempts 的 receipt coverage；不是完整性 gate。 |
| `aggregator_usage_accounting_observed`、`aggregator_usage_{physical_request,row,missing}_count` | stage observed、未 capped、每个 started attempt 有严格 v1 block，且逐 attempt 满足 `rows + missing = physical` | 全 attempt 物理调用与 usage 行守恒；允许 missing >0，但只输出精确 accounting totals。 |
| `aggregator_usage_projection_complete`、`aggregator_{input,output,reasoning,cache_read,cache_write}_tokens`、`aggregator_cache_hit_request_count` | accounting exact、missing=0、每个 started attempt 的五个 token fields 与 cache-hit count 完整且总和有界 | 所有失败、continuation、fallback 与最终成功 attempt 的 exact role totals。 |
| `aggregator_cost_source_observed`、`aggregator_cost_source_kind` | usage projection complete 且每个 started attempt 有固定 provenance enum | 固定 `provider_billed`、`mixed`、`unverified`、`none`；不保留 provider 自定义 token。 |
| `aggregator_cost_projection_complete`、`aggregator_billed_cost_usd_observed`、`aggregator_billed_cost_usd` | cost source 全部为 `provider_billed` 且有界求和成功 | 所有 aggregator attempts 的 exact provider-billed USD total。mixed/unverified/none 不输出成本值。 |
| `aggregator_final_request_usage_container_observed` | final request role 为 aggregator 且 usage 为 dict | 最后一次 aggregator request 的 usage gate。 |
| `aggregator_final_request_usage_projection_complete`、`aggregator_final_request_usage_observed` | 五个 token fields 与 billed cost 全部合法、至少一项非零、无 missing marker | final-request exact projection gate。 |
| `aggregator_final_request_{input,output,reasoning,cache_read,cache_write}_tokens_observed`、对应 value | projection complete | 最后一次 aggregator request totals。 |
| `aggregator_final_request_cache_hit_observed`、`aggregator_final_request_cache_hit` | projection complete | final request 是否 cached_tokens >0。 |
| `aggregator_final_request_billed_cost_usd_observed`、对应 value | projection complete | final request observed billed cost。 |
| `physical_request_count_observed`、`physical_request_count` | top-level scalar 合法 | 全 ensemble physical request total。 |
| `unknown_usage_count_observed`、`unknown_usage_count` | top-level usage missing scalar 合法 | 全 ensemble unknown usage total。 |

`aggregator_final_request_*` 保留为向后兼容和最终请求诊断；role totals 与成本 SLO 必须优先使用 `aggregator_usage_projection_complete` / `aggregator_cost_projection_complete`。v1 尚无 cache savings、非 USD currency 换算或 logical request count；这些仍 unavailable。`mixed`、`unverified`、`none` 只描述证据状态，绝不能把对应 `billed_cost_usd` 零值当作真实成本。

主要解决：仅在 receipt coverage 完整时计算 role totals，并阻止未知 usage 被零 token/零成本占位符稀释。

### 3.9 Canary rollout 与单次物理预算

| 字段族 | 输出条件与精度 | 语义 |
| --- | --- | --- |
| `canary_rollout_observed` | `selection_plan.canary_rollout.schema` 精确等于 `opensquilla.ensemble-canary-rollout/v1` | rollout receipt gate；未知 schema 不投影。 |
| `canary_rollout_projection_complete` | schema 已识别；enabled/config bool、input/admitted 闭合、完整 task gate、固定 risk、全部 12 个 reason counts 与 rollout conservation 均合法 | 可消费本 turn admission/reason counts 的总 gate。 |
| `canary_rollout_{enabled,config_valid}_observed`、对应 value | source 为 bool | 本 turn rollout 开关与 runtime config 重校验结果。 |
| `canary_rollout_input_canary_count_observed`、`canary_rollout_input_canary_count` | 严格非负整数 | 进入 rollout filter 的 canary 数。 |
| `canary_rollout_admitted_counts_observed`、`canary_rollout_{proposer,aggregator}_admitted_count` | 两个 role count 都是严格非负整数，且 proposer ≤ 1、aggregator = 0、role sum ≤ input count | 本 turn 按 role 放行的 canary 数；矛盾 counts 不输出。 |
| `canary_task_gate_observed` | task gate 为 dict | canary task gate receipt。 |
| `canary_task_{analyzer_source_eligible,schema_valid,confidence_eligible,eligible}_observed`、对应 value | source 为 bool | Analyzer 来源、schema、confidence 与总 gate。 |
| `canary_task_risk_observed`、`canary_task_risk` | 固定 `low`、`medium`、`high`、`unknown` | 固定风险枚举；其他 token 不输出。 |
| `canary_rollout_reason_counts_observed`、`canary_rollout_reason_{policy_invalid,rollout_disabled,decision_id_missing,task_ineligible,global_cohort_excluded,role_disabled,role_cohort_excluded,health_unhealthy,role_unsupported,reliability_coverage_insufficient,reliability_threshold_exceeded,candidate_cap}_count` | 全部固定 reason family 都有严格非负整数 | 各 gate family 的 count；不输出原始 reason 文本。 |
| `canary_rollout_conservation_observed`、`canary_rollout_conservation_valid` | enabled/config、完整 task gate、input/admitted 与全部 reason counts 足以做有界校验 | 要求 input ≥ 1、`sum(12 reason counts) == 2 × input_canary_count - proposer_admitted`、`role_disabled >= input`、enabled 蕴含 config_valid、task eligible 蕴含 source/schema/confidence 三项 eligible；且 proposer admitted > 0 时 enabled、config_valid、task eligible 必须全为 true。 |
| `canary_physical_budget_observed` | top-level budget schema 精确等于 `opensquilla.ensemble-canary-physical-budget/v1` | 单个 root decision 的 budget receipt gate。 |
| `canary_physical_budget_accounting_observed` | limit、committed、reserved、rejected、refunded 全为严格非负整数 | budget 字段类型完整。 |
| `canary_physical_budget_conservation_observed`、`canary_physical_budget_conservation_valid` | accounting observed | v1 要求 `limit == 1`、`committed + reserved <= limit`，且 `rejected > 0` 时 committed/reserved/refunded 至少一项 > 0；refunded/rejected 是累计结算事件，不进入 active balance。 |
| `canary_physical_budget_projection_complete`、`canary_physical_budget_{limit,committed,reserved,rejected,refunded}` | accounting 完整且 conservation valid | 可用于精确 per-turn budget 聚合。 |
| `canary_physical_budget_exhausted_observed`、`canary_physical_budget_exhausted` | projection complete；`rejected > 0` | 本 turn 是否至少有一次 reservation 被物理上限阻止。 |
| `canary_persistent_rollout_observed` | top-level schema 精确等于 `opensquilla.ensemble-canary-persistent-rollout/v1` | 同机持久 rollback receipt gate；未知 schema 不投影。 |
| `canary_persistent_rollout_projection_complete` | enabled 固定为 true；top-level 只有固定四字段；receipt count 与 ≤8 行完全一致；每行字段集合、固定枚举、布尔/整数和 admission→mutation→transition 守恒全部合法 | 只有此 gate 为真时才能消费下列 aggregate counts。0 receipt 是合法完整事件。 |
| `canary_persistent_rollout_{enabled,receipt_count}_observed`、对应 value | strict bool；严格非负且 ≤8 的整数 | 配置已接入和本 turn receipt 行数的字段级证据。 |
| `canary_persistent_rollout_{admission_allowed,admission_denied,admission_unavailable,probe,settled,cancelled_before_request,mutation_unavailable,rollback_transition,recovery_transition}_count` | projection complete | admission、settlement 与 durable state transition 的固定聚合；denied 不计入 cancelled-before-request。 |
| `canary_persistent_rollout_provider_{success,rate_limited,upstream_5xx,transport_failure,invalid_response,configuration_failure,unknown_failure}_count` | projection complete；只统计已越过物理边界并 settle 的 receipt | provider terminal outcome 固定枚举计数。 |
| `canary_persistent_rollout_usage_{observed,missing}_count` | projection complete；只统计 settled receipt | billable usage receipt 是否完整。 |

投影明确丢弃 policy/root hash、policy version、bucket、model/provider/deployment identity 和任意 reason 文本。畸形或未知 schema 只留下 observed/complete gate，不把不可信 count 当作 0 或 exact value。

`reason_counts` 统计的是 role-scoped exclusion occurrence，不是 unique canary。Producer 只有发现至少一个 canary 才写 rollout receipt；每个 input canary 固定产生一个 aggregator `role_disabled` occurrence，每个未 admitted proposer 再产生一个 proposer exclusion occurrence，所以总数必须满足上述守恒式，也可能大于 `input_canary_count`。因此禁止用 reason/input 计算 rejection rate；`role_disabled` 同时包含固定的 aggregator 禁用，不能直接解释为 proposer 配置故障。

Persistent receipt 证明同一台主机、同一 state directory 内多个 worker 共享的 SQLite
ledger 已完成 admission/settlement 及 rollback/recovery transition。它仍不证明跨主机
一致性；不支持 network filesystem；v1 quality gate 固定 unavailable；promotion、
false-rollback、post-rollback service health 与自动回滚延迟仍 unavailable。

主要解决：让 canary 是否被 gate、为何被固定家族拦截，以及单次物理请求硬上限是否生效可审计，同时不把 per-turn receipt 冒充持续 rollout 控制面。

## 4. Vendor-neutral dashboard panels

| Panel | 过滤与字段 | 展示 / 聚合 | Missing evidence 规则 | 主要解决 |
| --- | --- | --- | --- | --- |
| Outcome overview | schema v1；`execution_status` | event count，success/degraded/failed rate，按 `selection_family` 分面 | 只统计已摄取 v1 events；同时显示“producer delivery coverage unavailable” | 主要解决：快速发现 terminal failure 与 degraded delivery 上升。 |
| Latency percentiles | ranking 三阶段各自 observed value、`task_analyzer_elapsed_ms`、`time_to_quorum_ms`、exact role `admission_wait_ms_max`、exact `trace_compact_json_bytes` | 每个 scalar 分别画 p50/p95/p99；`proposer_candidate_elapsed_ms_max` 仅作 diagnostic | 字段缺失不入样本；ranking 联合视图还要求 projection complete；lower bound/capped 不混入 exact quantile | 主要解决：给现有可证明的阶段延迟建立尾延迟视图。 |
| Analyzer | Analyzer observed/source/schema/chain/deadline fields | source family、schema-valid、exhausted、expired rate；attempt/physical counts | capped chain 从 exact SLO 排除，单列 cap rate | 主要解决：定位 Analyzer fallback、schema 和 deadline 故障。 |
| Trace / snapshot | exact/lower-bound trace size、cap reason、selection family、runtime health filter、ranking stage observed/complete、packaged cache hit observed/value | size p50/p95/p99、cap rate、family mix、三阶段 coverage 与 cache-hit share | capped size只进入 lower-bound series；cache 缺失不补 false；version/hash 与非 packaged cache 继续 unavailable | 主要解决：控制 trace 膨胀并量化 snapshot 阶段/cache 证据覆盖。 |
| Admission | role observed/complete、exact/lower-bound counts/waits | proposer/aggregator 分开展示 exact failure rate、coverage、wait p50/p95/p99；lower bounds 单独堆叠 | projection incomplete 只进 coverage/lower-bound，不进 exact SLO | 主要解决：区分真实队列压力和无法 join 的不完整 admission 证据。 |
| Runtime health | filter、role health states、deferred、half-open、never-strand | state/defer shares、probe activity、never-strand activation、filtered/minimum/exempt counts | 无 observed gate 不入 rate；不推断 probe success | 主要解决：发现 benched/half-open 堆积和 never-strand 频繁兜底。 |
| Logical terminal HTTP | proposer/aggregator observed status counts | 429、5xx count 与 observed-status share，role 分开 | 没有 status 的 physical request 不入 denominator；注明不是真实 call error rate | 主要解决：发现 logical terminal 的 rate-limit 与 upstream burst，同时避免假分母。 |
| Quorum / cleanup / recovery | quorum、cleanup、proposer recovery fields | quorum reach、time-to-quorum p50/p95/p99、lingering、unproven close、recovery calls | 对应 observed/value 缺失即 unavailable | 主要解决：发现 quorum 后仍残留 worker/stream 或额外 physical calls。 |
| Aggregator | stage、attempt kinds/outcomes、selected kind、physical counts、recovery state | stage success/degraded/exhausted、outcome mix、selected-kind mix、physical requests | 空预初始化 recovery block 不进 stage denominator；capped attempts 不进总量 SLO | 主要解决：把 aggregator recovery 的质量损失和调用放大可视化。 |
| Role usage / cost / cache | proposer projection complete；aggregator all-attempt accounting/usage/cost complete；final-request 仅诊断；top-level unknown | tokens、unknown ratio、exact provider-billed cost、cache-hit share、coverage 与 cost-source mix | 不完整 role totals 不补零；aggregator mixed/unverified/none 不进 exact cost；final-request 不代替全 attempt totals | 主要解决：在费用与 cache 面板中保留 usage 与 cost-source 证据完整性。 |
| Canary rollout / physical budget | rollout/task/reason observed fields；rollout/budget conservation 与 complete；budget exhausted | input/admitted、task gate、固定 reason family、config invalid、budget rejection/refund 与两类 conservation | admission/reason sums 只消费 rollout complete；budget totals 只消费 budget complete；字段级 rate 使用各自 observed gate；不展示 hash、bucket 或 identity | 主要解决：发现 canary gate 漂移、配置失效和单次物理上限异常。 |
| Canary persistent rollback | persistent observed/complete；admission、settlement、provider/usage outcome、rollback/recovery transition counts | projection coverage、ledger unavailable、usage missing、rollback/recovery transition 与固定 provider outcome trends | aggregate 只消费 persistent complete；denominator 为 0 时 unavailable；严禁把 policy/deployment hash、provider/model 或 token 变成 label | 主要解决：观测同机持久 safety latch 是否真实阻断、落闩与恢复，而不泄露 identity。 |
| Data quality / privacy | 所有 observed、projection complete、scan capped、trace cap、unexpected keys | coverage rates、cap rates、字段类型错误计数（由 ingestion 层）、cardinality | denominator 只用对应 evidence gate；禁止采集原始 trace | 主要解决：让 SLO 数据质量本身可审计。 |
| Canary rollback residuals | 无跨主机一致性、quality outcome、decision timestamp 或 post-rollback health | 已实现的 same-host counts 正常展示；multi-host/quality/latency/false-rollback 面板显示 `unavailable` | 禁止用 rollout rejection、budget exhausted、`fallback_used` 或 aggregator fallback 补齐残余指标 | 主要解决：区分已实现的持久安全闩与尚无证据的 rollout 质量闭环。 |

## 5. SLI / SLO 公式

定义窗口 `W`；`count(P)` 是满足 predicate `P` 的已摄取 v1 event 数，`sum(x | P)` 和 `quantile_q(x | P)` 只使用字段存在且 gate 为真的 events。任何 denominator 为 0 的结果必须是 unavailable，不得返回 0% 或 100%。所有下述目标都是初始建议，应在 2–4 周基线后校准。

### 5.1 Outcome

```text
delivery_rate = count(execution_status in {success, degraded}) / count(execution_status exists)
failure_rate  = count(execution_status == failed) / count(execution_status exists)
degraded_rate = count(execution_status == degraded) / count(execution_status exists)
```

建议：`delivery_rate >= 99.0%`、`failure_rate < 1.0%`、`degraded_rate < 5.0%`。这是 telemetry-conditional SLO；没有 producer delivery coverage 时不能升级为外部 availability SLA。

主要解决：用互斥终态衡量可交付性，同时保留 degraded 与 hard failure 的差异。

### 5.2 p50 / p95 / p99 latency

```text
analyzer_latency_q  = quantile_q(task_analyzer_elapsed_ms | field exists)
quorum_latency_q    = quantile_q(time_to_quorum_ms | field exists)
proposer_max_q      = quantile_q(proposer_candidate_elapsed_ms_max | field exists and candidate_scan_capped == false)
role_admission_q    = quantile_q(role_admission_wait_ms_max | role_admission_projection_complete == true)
trace_size_q        = quantile_q(trace_compact_json_bytes | trace_size_observed == true and capped == false)
snapshot_build_q    = quantile_q(ranking_snapshot_build_ms | ranking_snapshot_build_ms_observed == true)
hard_filter_q       = quantile_q(ranking_hard_filter_ms | ranking_hard_filter_ms_observed == true)
ranking_score_q     = quantile_q(ranking_score_ms | ranking_score_ms_observed == true)

ranking_stage_evidence_coverage =
  count(ranking_stage_projection_complete == true)
  / count(ranking_stage_observed == true)

packaged_template_cache_hit_rate =
  count(ranking_packaged_template_cache_hit == true)
  / count(ranking_packaged_template_cache_hit_observed == true)
```

建议由部署方配置固定预算 `T_snapshot`、`T_filter`、`T_score`、`T_analyzer`、`T_quorum`、`T_admission`：p95 不超过对应预算，p99 不超过 1.5 倍预算；没有预算时只做 7 天同小时基线告警（warning > 2× baseline，critical > 3× baseline，且至少 20 个样本）。ranking stage evidence coverage warning `<99%`、critical `<95%`；cache-hit rate 只作容量/性能趋势，不设通用成功阈值。`proposer_max_q` 是 per-call max 的分布，不是 candidate latency histogram。overall ensemble latency 与 aggregator attempt latency 当前 unavailable。

主要解决：提供不依赖具体监控厂商的尾延迟公式，并避免把 prefix/max 指标误称为逐请求延迟。

### 5.3 Unknown usage

```text
ensemble_unknown_usage_rate =
  sum(unknown_usage_count | unknown_usage_count_observed and physical_request_count_observed)
  / sum(physical_request_count | same events)

proposer_unknown_usage_rate =
  sum(proposer_unknown_usage_count | proposer_unknown_usage_count_observed and proposer_physical_request_count_observed)
  / sum(proposer_physical_request_count | same events)
```

建议：warning `>0.1%` 持续 15 分钟，critical `>1.0%` 持续 10 分钟。Aggregator 全 attempts 的 unknown usage rate unavailable；final-request usage incomplete 只能进入 coverage 告警。

主要解决：避免未知 usage 被默认 0 token/0 cost 稀释，从而暴露费用与容量盲区。

### 5.4 Admission

对 `role ∈ {proposer, aggregator}`：

```text
role_admission_failure_rate =
  sum(role_timeout_count + role_rejected_count | role_projection_complete)
  / sum(role_admitted_count + role_timeout_count + role_rejected_count | role_projection_complete)

role_admission_evidence_coverage =
  count(role_projection_complete == true)
  / count(role_admission_observed == true)
```

建议：failure rate warning `>1%`、critical `>2%`；evidence coverage warning `<99%`、critical `<95%`。所有 `_lower_bound` series 只用于“至少发生”告警，不进入上述公式。

主要解决：将 admission SLO 限定在一对一完整证据，防止重复或漏行制造虚假成功率。

### 5.5 Logical-terminal HTTP

```text
role_429_share_of_observed_status =
  sum(role_logical_terminal_rate_limited_count)
  / sum(role_logical_terminal_http_status_observation_count)

role_5xx_share_of_observed_status =
  sum(role_logical_terminal_upstream_5xx_count)
  / sum(role_logical_terminal_http_status_observation_count)
```

这两个公式是“已观察 error status 的构成”，不是全调用 429/5xx rate。建议只有窗口中 observed statuses ≥20 时评估：warning share `>25%`，critical `>50%`；无论 denominator，5 分钟 count 超过 `max(5, 3×七日同小时基线)` 时触发 burst 告警。真实 HTTP error-rate SLO 在 v1 unavailable。

主要解决：对 429/5xx burst 提供诚实分母，不把缺失 HTTP status 当作成功。

### 5.6 Runtime health、half-open 与 never-strand

```text
proposer_half_open_share =
  sum(proposer_runtime_health_half_open_count)
  / sum(proposer_runtime_health_state_observation_count)

proposer_probe_share =
  sum(proposer_runtime_health_probe_count)
  / sum(proposer_runtime_health_probe_observation_count)

never_strand_activation_rate =
  count(runtime_health_never_strand == true)
  / count(runtime_health_never_strand_observed == true)
```

建议：任一 share/activation rate 超过七日同小时基线 3 倍且至少 20 个 observations 时 warning；`never_strand_activation_rate >10%` 持续 15 分钟时 critical。half-open probe success rate、never-strand violation rate 当前 unavailable。

主要解决：发现健康闭环频繁进入试探或兜底状态，同时不虚构 probe 成功证据。

### 5.7 Quorum、cleanup 与 recovery

```text
quorum_reach_rate =
  count(quorum_reached == true) / count(quorum_reached_observed == true)

cleanup_lingering_event_rate =
  count(cleanup_lingering_task_count > 0)
  / count(cleanup_observed == true and cleanup_lingering_task_count exists)

stream_close_unproven_share =
  sum(cleanup_stream_close_unproven_count)
  / sum(cleanup_stream_close_proven_count + cleanup_stream_close_unproven_count)

mean_proposer_recovery_calls =
  sum(proposer_recovery_calls | proposer_recovery_observed)
  / count(proposer_recovery_observed == true)
```

建议：`quorum_reach_rate <99%` warning、`<95%` critical；任何 lingering 或 unproven stream close 连续两个 5 分钟窗口大于 0 即 critical；recovery calls 超过七日同小时均值 3 倍且样本 ≥20 时 warning。

主要解决：把 quorum 质量、取消清理和额外 physical recovery 放在同一故障链上。

### 5.8 Aggregator

```text
aggregator_recovery_success_rate =
  count(aggregator_recovery_success == true)
  / count(aggregator_stage_observed and aggregator_recovery_success_observed)

aggregator_recovery_degraded_rate =
  count(aggregator_recovery_degraded == true)
  / count(aggregator_stage_observed and aggregator_recovery_degraded_observed)

aggregator_unsuccessful_attempt_share =
  sum(aggregator_unsuccessful_attempt_count | attempt_scan_capped == false)
  / sum(aggregator_recovery_attempt_scan_count | attempt_scan_capped == false)
```

建议：recovery success `<95%` warning、`<90%` critical；degraded 或 unsuccessful share `>5%` warning、`>10%` critical。空的预初始化 recovery block和 capped attempts 不进入 denominator。

主要解决：避免“存在 recovery block”被误当成 aggregator 真正执行或成功。

### 5.9 Role usage、cost 与 cache

```text
proposer_cache_hit_request_rate =
  sum(proposer_cache_hit_request_count | proposer_usage_projection_complete)
  / sum(proposer_usage_row_count | proposer_usage_projection_complete)

proposer_billed_cost_per_eligible_event =
  sum(proposer_billed_cost_usd | proposer_usage_projection_complete)
  / count(proposer_usage_projection_complete == true)

aggregator_cache_hit_request_rate =
  sum(aggregator_cache_hit_request_count | aggregator_usage_projection_complete)
  / sum(aggregator_usage_row_count | aggregator_usage_projection_complete)

aggregator_billed_cost_per_eligible_event =
  sum(aggregator_billed_cost_usd | aggregator_cost_projection_complete)
  / count(aggregator_cost_projection_complete == true)

aggregator_unknown_usage_rate =
  sum(aggregator_usage_missing_count | aggregator_usage_accounting_observed)
  / sum(aggregator_usage_physical_request_count | aggregator_usage_accounting_observed)
```

Tokens 使用对应 usage complete gate；aggregator billed cost 只使用更严格的 cost complete gate做 sum/mean/p50/p95/p99。建议 usage/accounting projection coverage `<99%` warning、`<95%` critical；费用和 cache 先采用七日基线告警。只有 `provider_billed` aggregator events 可进入 exact 成本预算；mixed/unverified/none 单列 coverage，不补零。

主要解决：让 token、cache 和 billed cost 的趋势可用，同时阻止不完整 receipts 形成虚假成本下降。

### 5.10 Canary rollout 与物理预算

```text
canary_config_valid_rate =
  count(canary_rollout_config_valid_observed == true and canary_rollout_config_valid == true)
  / count(canary_rollout_config_valid_observed == true)

canary_task_eligible_rate =
  count(canary_task_eligible_observed == true and canary_task_eligible == true)
  / count(canary_task_eligible_observed == true)

canary_proposer_admission_share =
  sum(canary_rollout_proposer_admitted_count | canary_rollout_projection_complete)
  / sum(canary_rollout_input_canary_count | canary_rollout_projection_complete)

canary_budget_conservation_rate =
  count(canary_physical_budget_conservation_valid == true)
  / count(canary_physical_budget_conservation_observed == true)

canary_rollout_conservation_rate =
  count(canary_rollout_conservation_valid == true)
  / count(canary_rollout_conservation_observed == true)

canary_budget_exhaustion_rate =
  count(canary_physical_budget_exhausted == true)
  / count(canary_physical_budget_exhausted_observed == true)
```

`canary_proposer_admission_share` 只在 rollout projection complete 且 input sum > 0 时计算；它是筛选比例，不是请求成功率。固定 reason family counts 也只在 projection complete events 中按字段求和，并与 input/admitted 趋势并列展示，不能转回原始 reason label。

建议：config valid、rollout conservation 与 budget conservation 必须为 100%；任一 conservation false 立即 critical，任一 config invalid 在 config-valid observed 时立即 warning、连续两个 5 分钟窗口 critical。Budget exhaustion 表示硬上限成功阻止额外 reservation，先采用七日同小时基线，只在突然升高时 warning，不把它本身定义为业务失败。task eligibility 与 admission share 先做趋势，不设 promotion SLO。

主要解决：验证 rollout 输入、固定 gate 与一请求硬上限确实按 producer 合同运行。

### 5.11 Same-host persistent canary rollback

```text
persistent_projection_coverage =
  count(canary_persistent_rollout_projection_complete == true)
  / count(canary_persistent_rollout_observed == true)

persistent_admission_unavailable_rate =
  sum(canary_persistent_rollout_admission_unavailable_count | projection_complete)
  / sum(canary_persistent_rollout_receipt_count | projection_complete)

persistent_mutation_unavailable_rate =
  sum(canary_persistent_rollout_mutation_unavailable_count | projection_complete)
  / sum(canary_persistent_rollout_admission_allowed_count | projection_complete)

persistent_usage_missing_rate =
  sum(canary_persistent_rollout_usage_missing_count | projection_complete)
  / sum(canary_persistent_rollout_settled_count | projection_complete)

persistent_rollback_transition_rate =
  sum(canary_persistent_rollout_rollback_transition_count | projection_complete)
  / sum(canary_persistent_rollout_settled_count | projection_complete)
```

上式中的 `projection_complete` 均指
`canary_persistent_rollout_projection_complete == true`。建议 projection coverage 为
100%；admission 或 mutation unavailable 任一非零立即 critical；usage missing 任一
非零立即 warning。Rollback transition 是安全闩触发趋势，不是业务失败率，也不能
作为 promotion SLO。Recovery transition 单独显示 count，不以 canary 请求数构造
“恢复成功率”。

同机 ledger 已关联 physical attempt outcome、usage evidence 与 durable
rollback/recovery transition，但没有可投影的 latch timestamp、可信 quality outcome、
跨主机共识或 post-rollback service health。因此 automatic rollback latency、false
rollback rate、quality rollback rate 和 multi-host rollout safety 仍 unavailable。
`canary_physical_budget_exhausted`、普通 rollout rejection、`fallback_used`、
`aggregator_selected_kind` 和 model fallback 都不能替代这些指标。

主要解决：为已实现的 same-host 持久安全闩提供低基数闭环，同时明确剩余的
multi-host、quality 与 latency 证据缺口。

## 6. 告警阈值建议

| 告警 | Warning | Critical | 最小证据 gate | 主要解决 |
| --- | --- | --- | --- | --- |
| Terminal failure | `failure_rate >1%` / 15m | `>2%` / 10m | ≥100 v1 events | 主要解决：发现 hard failure 上升。 |
| Degraded delivery | `degraded_rate >5%` / 30m | `>10%` / 15m | ≥100 v1 events | 主要解决：发现 fallback/partial delivery 被成功率掩盖。 |
| Analyzer deadline | expired `>0.1%` / 15m | `>1%` / 10m | deadline expired observed ≥20 | 主要解决：发现 Analyzer 预算耗尽。 |
| Evidence cap | 任一 scan/trace cap rate `>0.1%` / 30m | `>1%` / 15m | 对应 observed ≥100 | 主要解决：发现 trace 或 attempt 数超出可审计范围。 |
| Admission failure | exact rate `>1%` / 15m | `>2%` / 10m | projection complete 且 exact denominator ≥20 | 主要解决：发现 queue timeout/rejection。 |
| Admission evidence | coverage `<99%` / 30m | `<95%` / 15m | role observed ≥100 | 主要解决：发现 attempt join 丢失。 |
| Unknown usage | `>0.1%` / 15m | `>1%` / 10m | top-level physical+unknown observed，或 aggregator usage accounting observed | 主要解决：发现 token/cost 盲区。 |
| Logical HTTP burst | count `>max(5,3×baseline)` / 5m | count `>max(20,5×baseline)` / 5m | observed status count；share 需 ≥20 | 主要解决：发现 429/5xx 突发而不制造全调用 error rate。 |
| Quorum reach | `<99%` / 15m | `<95%` / 10m | quorum reached observed ≥20 | 主要解决：发现 proposer quorum 退化。 |
| Cleanup | 任一 lingering/unproven >0 两个窗口 | 任一 >0 持续 10m | cleanup/value observed | 主要解决：发现 worker 或 stream 未收尾。 |
| Runtime health | state/defer/never-strand `>3×baseline` | never-strand `>10%` / 15m | 对应 observed ≥20 | 主要解决：发现 health filter 频繁兜底。 |
| Aggregator recovery | success `<95%` 或 degraded `>5%` | success `<90%` 或 degraded `>10%` | stage + terminal bool observed，≥20 | 主要解决：发现 aggregator recovery 不稳定。 |
| Usage projection | complete coverage `<99%` / 30m | `<95%` / 15m | role container observed ≥100 | 主要解决：发现 role cost/cache 面板失真风险。 |
| Canary config | 任一 config invalid | 连续两个 5m 窗口存在 invalid | rollout/config-valid observed | 主要解决：发现 runtime canary policy 重校验失败。 |
| Canary rollout conservation | 任一 conservation false | 立即 critical | rollout conservation observed | 主要解决：发现 admission、固定 reason counts 或 gate 状态自相矛盾。 |
| Canary budget conservation | 任一 conservation false | 立即 critical | budget accounting/conservation observed | 主要解决：发现一请求硬上限 receipt 自相矛盾。 |
| Canary budget exhaustion | `>3×七日同小时基线` | 只人工升级，不自动 rollback | exhausted observed ≥20 | 主要解决：发现额外 canary reservation 突增，同时避免把安全拒绝当失败。 |
| Canary persistent rollback availability | admission 或 mutation unavailable 任一非零 | 任一非零立即 critical | persistent projection complete 且 receipt count >0 | 主要解决：发现同机持久 safety ledger 无法可靠 admission/settle；不把 transition count 误作失败率或延迟。 |

## 7. Privacy、retention 与 cardinality gates

1. Ingestion allowlist 只能接受本契约字段；未知字段先隔离并审查，不能自动成为 label。主要解决：阻止未来 trace 字段绕过低基数边界。

2. 禁止采集 model/provider/deployment identity、prompt、output、reasoning、error text、response id、tenant/session/task/user id；也禁止把完整 `selection_plan`、`candidates`、`final_request` 或 `aggregator_recovery` 当作日志属性。主要解决：防止敏感内容和高基数 identity 进入指标系统。

3. 可作 label 的 producer 字段仅限 `schema`、`terminal_outcome`、`execution_status`、`selection_family`，以及各自专用 series 中最多一个固定 enum（`task_analyzer_source_family`、`aggregator_selected_kind`、`aggregator_cost_source_kind`、`trace_compact_json_bytes_cap_reason` 或 `canary_task_risk`）；所有 bool、counts、indices、bytes、tokens、cost 和 latency 必须是 value。Canary policy/root hash、bucket、model/provider/deployment identity 和 reason 文本一律禁止采集。主要解决：固定时序数据库的 label cardinality 上界，避免多个低基数字段相乘后失控。

4. 未知枚举统一落入 producer 的 `unknown` 或 unavailable；下游不得保留原始 token作为新枚举值。主要解决：避免供应商错误码或模型名形成无限 label domain。

5. 建议 raw metrics events 保留 14 天，5 分钟 role/schema rollups 保留 90 天，SLO 日级 rollups 保留 13 个月；延长 raw retention 必须通过隐私和存储复审。主要解决：在故障排查窗口与长期低风险趋势之间取得平衡。

6. Dashboard 和 alert query 必须显式实现“field missing → unavailable”；禁止 `coalesce(missing, 0)`，禁止把 `_lower_bound` 与 exact series 相加。主要解决：从查询层封死假分母和假成功。

7. 每个 SLO 同时展示 evidence coverage、scan cap rate 和样本数；样本不足时暂停 burn-rate 判断。主要解决：让值的可信度与值本身一起可见。

8. 日志后端或 processor 失败是 fail-open，且 v1 gate 会把本次调用标记为已经尝试 emission；当前不能从 v1 events 反推丢失量。主要解决：明确 telemetry delivery 不是 exactly-once durable delivery。

## 8. 落地验收清单

1. 只订阅事件名 `llm_ensemble.execution.metrics` 且 schema 为 v1，其他事件进入隔离流。主要解决：防止 schema 混算。

2. 按字段字典做类型、枚举和 allowlist 校验，保留 missing，不补 0。主要解决：保证不同 dashboard 后端得到同一数据语义。

3. 所有 exact SLO query 都带 observed/projection-complete/scan-cap gate，并给 denominator=0 返回 unavailable。主要解决：消除缺失证据对 SLO 的污染。

4. `_lower_bound`、prefix counts 和 diagnostic totals 使用独立 series 名与面板颜色，不能和 exact series stack。主要解决：避免视觉上把下界误认为总量。

5. Dashboard 至少包含第 4 节全部 panels，并在 unavailable panel 中显示所缺 producer evidence。主要解决：让已覆盖能力和遥测缺口同时可见。

6. 在 2–4 周基线期只启用 data-quality、cleanup 和极端 failure 告警，随后再校准第 6 节百分比阈值。主要解决：减少无基线情况下的告警噪音。

7. Same-host automatic rollback 只使用已审计的持久 ledger receipt 与本节 persistent complete gate；启用前必须满足本地 `state_dir`、精确 status/reset 运维流程和同机 worker 共享数据库约束。当前 receipt 不提供 latch timestamp、可信 quality outcome、跨主机共识或 post-rollback service health，因此 automatic rollback latency、false-rollback、quality 与 multi-host SLO/自动告警继续禁用。主要解决：既允许消费已经落地的同机 safety signal，又避免把 per-turn rollout/budget receipt、ensemble fallback 或 transition count 伪装成尚不存在的安全 SLO。
