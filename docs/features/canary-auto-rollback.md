# Persistent canary auto-rollback（same-host v1）

状态：v1 已接入 live `router_dynamic` proposer 的物理请求边界，默认关闭。它使用
同一 `state_dir/canary_rollout.sqlite3` 在单机多个 gateway worker 之间共享
admission、attempt outcome、rollback latch 与 half-open recovery 状态。Static、
frozen replay、formal/experiment 路径不创建或访问该 ledger。

## 配置

以下是最小启用示例；上线前仍需完成 canary registry、Analyzer task gate、runtime
health 和普通 single-request budget 的既有配置：

```toml
[llm_ensemble]
enabled = true
selection_mode = "router_dynamic"

[llm_ensemble.canary_rollout]
enabled = true
global_basis_points = 100

[llm_ensemble.canary_rollout.proposer]
basis_points = 100
max_candidates_per_decision = 1
min_observations = 20
max_failure_basis_points = 500

[llm_ensemble.canary_rollout.auto_rollback]
enabled = true
window_max_attempts = 50
window_max_age_seconds = 300
max_consecutive_provider_failures = 3
max_rate_limited_count = 0
max_usage_missing_count = 0
rollback_cooldown_seconds = 3600
half_open_successes_required = 3
half_open_probe_spacing_seconds = 30
half_open_probe_lease_seconds = 3600
active_attempt_lease_seconds = 3600
scope_retention_seconds = 604800
max_scopes = 1024
max_pending_per_scope = 64
```

`enabled` 必须是严格 boolean。Window 必须覆盖 proposer 的
`min_observations`；cooldown 必须覆盖 active attempt lease；retention 必须覆盖
window age。v1 只允许 proposer canary，quality gate 尚未接入可信 evaluator，固定为
unobserved，不能用“被 aggregator 选中”冒充 quality pass。

## 请求边界与失败模式

每个 managed canary 的顺序固定为：fresh runtime-health gate → root physical budget
reserve → persistent ledger `begin_attempt` → 紧邻 `provider.chat()`。Ledger deny、
busy、schema/contract mismatch 或 corruption 一律 fail closed，refund root budget，且
物理请求数保持 0；不会退化到进程内临时账本。只有可证明 `request_started=false`、
physical count 为 0 且底层 iterator 已关闭的路径才 cancel token。任何可能越过物理
边界的成功、错误、timeout、outer cancellation、incomplete stream 或 close-unproven
都必须 exactly-once settle；worker crash 由 lease expiry 持久落闩。

Provider outcome 是固定枚举：success、rate limited、upstream 5xx、transport failure、
invalid response、configuration failure、unknown failure。完整 usage receipt 标为
observed；越界后 usage 不完整标为 missing。429、configuration failure、usage
missing、连续失败、failure-rate threshold、abandoned attempt/probe 都可按 policy
触发 durable rollback。Configuration failure 和 policy-contract mismatch 只允许
人工 reset；其他 latch 在 cooldown 后只允许一个跨 worker half-open probe，连续 K
个完整成功才恢复。

数据库使用 owner-only file、WAL、`synchronous=FULL` 和 `BEGIN IMMEDIATE`。Schema、
enum、policy contract、clock watermark、attempt lease、recovery epoch/ordinal proof 任一
不一致都会 sticky fail closed；程序不会自动删除或重建可疑数据库。

## 精确 status / reset

查看当前配置对应的一个精确 scope：

```bash
opensquilla router canary-rollout-status \
  --role proposer \
  --provider openrouter \
  --model example/canary-model \
  --upstream google \
  --json
```

查看历史 policy scope 时显式传入完整的小写 SHA-256：

```bash
opensquilla router canary-rollout-status \
  --role proposer --provider openrouter --model example/canary-model \
  --policy-sha256 <64-lowercase-hex>
```

Reset 必须采用 stop → status → exact reset → restart 流程：

```bash
opensquilla gateway stop
opensquilla router canary-rollout-status \
  --role proposer --provider openrouter --model example/canary-model
opensquilla router canary-rollout-reset \
  --role proposer --provider openrouter --model example/canary-model --yes
opensquilla gateway start
```

Reset 在整个事务期间持有 gateway process lock；gateway 正在运行或 lock busy 时
拒绝。命令只接受精确 role/provider/model/upstream/policy scope，没有 wildcard、
`--all` 或 reset-database。输出只含固定状态、计数及 policy/deployment hash 前缀，
不输出 provider credentials、token 或 raw identity evidence。Contract-mismatch worker
已经 sticky unavailable，即使离线 reset 成功也必须 restart。

## 部署与观测边界

所有协作 worker 必须位于同一台主机并使用同一个本地 `state_dir`。SQLite v1 不支持
多主机协调，也不支持 network filesystem；这两种部署必须保持 auto rollback
disabled。`state_dir` 在 enabled 期间不能热切换，检测到漂移后进程会保持 fail
closed 直到重启。

Terminal trace 只写固定低基数 receipt，token、policy/deployment hash、provider/model
identity 都不会进入 receipt 或 metrics。对应 aggregate 字段和 denominator 规则见
`ensemble-execution-metrics-dashboard-slo.md`。当前没有 rollout quality、false
rollback、post-rollback service health 或 rollback latency 的可信 producer evidence；
这些指标继续 unavailable。
