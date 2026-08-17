# Router Dynamic 的 KV Cache 亲和设计

- 状态：设计方案，尚未修改生产代码
- 基线：`962666faae62eea5863af25562922c0659c314ea`
- Worktree：`/home/codex/code/opensquilla-dev-20260814`
- 日期：2026-08-17

## 1. 结论

不要把 KV cache 做成硬路由或模型锁定。方案只在同一 session 被高置信判定为
`continue` 时，利用上一轮成功物理请求的缓存证据，调整同角色、同 cache domain
候选的排序分数。

```text
原有硬过滤
  -> strategy=bonus：按配置增加小幅软分
  -> strategy=expected_cost：按 cache token 重算预期输入成本
  -> 原有 single Top-1 或 multiple proposer/aggregator 排名
```

缓存偏好不能绕过能力、健康、凭证、上下文、canary 或质量门槛；命不中缓存也不影响
正确性。
它同时支持 `selection_mode=router_dynamic` 下的两种执行拓扑：

- `mode=single`：只影响 direct-eligible 候选的 Top-1 排序；
- `mode=multiple`：分别影响 proposer 与 aggregator 排序。

状态角色严格区分为 `single`、`proposer`、`aggregator`，切换拓扑时不得跨角色复用。
配置块缺失即完全关闭：不收集 receipt、不构造 cache guard、不增加 trace 字段，也不改变
原有 ranking bytes/hash、selection plan 或执行链路。

## 2. 当前实现可直接复用

现有代码已经具备：

- `runtime.py::_router_dynamic_last_routes`：按 session 保存上一轮成功的 `selected_P/A`，有容量上限；
- `ranking_router.py::_session_score()`：`continue` 时给上一轮模型弱粘性，`redo` 时降权；
- `DoneEvent/model_usage_breakdown`：已有物理请求 cache token 与角色信息，可作为生成
  成功 receipt 的底层证据；
- OpenRouter upstream pin 与 credential pool 的 session pin；
- compaction 通知和 cache-break 诊断。

缺口是：上一轮状态只有 `provider:model`，没有执行拓扑、角色、时间、cache domain 和已观测
缓存证据；当前弱粘性也不能精确区分 direct single、proposer 与 aggregator。

## 3. 最小行为

### 3.1 记录缓存亲和

只在一轮 router-dynamic 执行完整成功、stream 已关闭、usage 完整后，从已执行的
物理 usage 行生成内存态：

```text
cache_affinity = {  # 仅进程内
  single: {identity, cache_domain_guard, evidence_kind, cached_tokens,
           cache_write_tokens, observed_at_monotonic},
  proposers: [{identity, cache_domain_guard, evidence_kind, cached_tokens,
               cache_write_tokens, observed_at_monotonic}],
  aggregator: {identity, cache_domain_guard, evidence_kind, cached_tokens,
               cache_write_tokens, observed_at_monotonic}
}
```

其中：

- `identity = provider:model`；
- `cache_domain_guard` 是“cache namespace 相同”的保守证明，不是命中保证。它对称
  绑定 `session_epoch`、请求前已解析的 provider/requested model、canonical
  endpoint（scheme/host/port 及 normalized path）、strict upstream 和 credential namespace；
- endpoint 含 userinfo 或 query 时首版 fail closed，不建立亲和，避免同 origin 下不同
  代理租户/部署被误认为同一 cache domain；
- credential resolver 新增仅进程内的 `credential_namespace_token`：在凭证选定边界用
  进程生命周期单例的随机 key，对版本、provider namespace、当前 resolved secret，以及实际
  会发到上游且改变 tenant/account cache namespace 的认证维度做 length-prefixed 无歧义编码
  后执行 keyed HMAC。当前至少纳入 `ProviderConfig.org_id`；未来新增 project/account header 时
  必须同步纳入，不能只比较 API key。随后立即丢弃 secret 输入，只把 opaque token 交给
  readiness/guard。token 字段必须 `repr=False` 且排除所有序列化；raw API key/`SecretStr`、
  org/project id 不进入 affinity state、trace 或 log。secret 或 tenant 维度变化会自然得到新
  token，现有 pin failure/rotation 路径同时主动 purge；进程重启后 HMAC key、token 和内存
  affinity 一起失效；
- `actual_model` 只用于上一轮 receipt 验真；先按冻结的 provider alias
  canonicalization 规则规范化，若仍与 requested model 不一致则不产生 receipt。
  这是有意的 fail-closed false negative；
- OpenRouter 仅在 upstream 严格固定且禁止 fallback 时建立亲和；`auto`、普通 order 或
  无法证明实际 endpoint 时不建立；门禁校验实际 request policy 的 strict pin
  与 `allow_fallbacks=false`，不只看 model-to-upstream 映射；
- `read_hit` 表示该物理请求 `cached_tokens > 0`；
- `write_only` 表示仅有 `cache_write_tokens > 0`，证明已创建缓存，但不保证下轮命中；
- credential pool 继续使用现有 session pin，不记录或输出密钥；
- 状态放在独立、有界的 `_router_dynamic_cache_affinity` 进程内 LRU，不复用
  `_router_dynamic_last_routes`，也不新增数据库。

不能让 runtime 直接猜测 `model_usage_breakdown` 中哪一行成功。多模型路径由
`EnsembleProvider` 在物理 stream 关闭且 effective selection plan 已确定后生成 receipt；
单模型路径由 `_RouterSingleDirectProvider` 在 direct `Done` 的 physical terminal boundary
与 health success 被证明后生成 receipt。当前 HEAD 中已观察到 terminal `Done` 本身就是
physical close proof，其后的 `aclose` 只是 best-effort；不能因为 optional `aclose` 失败误丢
receipt。未观察到 terminal/EOF 时仍执行 required `aclose`，但因为没有成功 `Done`，本来就
不得产生 receipt。两条路径复用同一个私有 receipt builder 和同一套 usage 校验：

```text
{
  physical_attempt_id, executed_role, execution_slot,
  requested_identity, actual_identity,
  cache_domain_guard, evidence_kind, cached_tokens, cache_write_tokens,
  observed_at_monotonic, ok=true
}
```

`executed_role` 只能是 `single | proposer | aggregator`。单模型 wrapper 在每次 `chat()`
物理 dispatch 前创建私有 `physical_attempt_id`；它不进入公开事件或 trace。Agent 工具循环
中的每次 `chat()` 都是独立 batch，只有与本轮最终成功 direct execution 对齐的 receipt
才可提交。多模型仍按最终 effective P/A execution receipt 对齐。

multiple collector 只从 `_canonicalize_usage_row()` 后的顶层字段取 token；single collector
只从 `_RouterSingleDirectProvider` 截住的原始 Provider `Done` 顶层 canonical 字段取 token。
两者都自行严格校验为 exact int、non-bool、`>=0`。multiple 的 `usage_reported=true`
必须由对应 `_CandidateResult`/aggregator attempt 生命周期证明；single 则要求原始 Provider
`Done` 的 canonical cache 字段通过同一严格校验。两者都拒绝 missing/unknown placeholder，
并按非空 `physical_attempt_id` 去重。
禁止再解析 raw `provider_usage`、使用 ensemble/session 汇总值或估算值。分类优先级固定为：

```text
cached_tokens > 0       -> read_hit
cache_write_tokens > 0 -> write_only
其他或供应方无法区分 read/write -> 无缓存证据
```

receipt 记录该物理请求终态时的私有 `observed_at_monotonic`。collector 是每次
provider `chat()` 的局部 one-shot batch，不是 provider 实例全局容器；provider
实例只保存 runtime 注入的私有 callback，多模型 retry factory 重建 provider 时必须透传它。
只有当次 async generator 和物理 stream 都证明闭合后，callback 才把 batch 交给
runtime 的私有 sidecar。
当前 HEAD 在 router-dynamic 的 proposer 和 aggregator 路径都会因
`_router_dynamic_selection()` 生成 physical attempt ID；collector 对 thinking 开/关两条路径都要求
该 ID 存在，不能自行补造。

sidecar 以 `turn_id + decision_id + provider_instance_token + chat_call_id` 绑定，并设
active-token 拒绝旧 provider 的延迟 callback。commit 只一次性 drain 最终 chat call 中、
与 effective execution 精确对齐的 batch；对齐使用私有
`physical_attempt_id + role + slot + requested/actual identity`，不只按 model identity 猜测。
所有终态都必须清理 collector/sidecar。receipt 不放入 `DoneEvent`、
`turn.metadata` 或持久 trace；
失败、partial、fallback、usage 缺失或 stream 未闭合的请求不产生 receipt。

### 3.2 配置：不允许隐藏 magic number

在 ranking config 的 `session` 下增加一个**可选、严格判别式**对象
`kv_cache_affinity`。整个对象缺失即关闭；实现不得在代码中补 bonus、TTL、命中概率等
数值默认值。可写入版本化 `router_dynamic_ranking_config.json`，也可通过现有
`llm_ensemble.ranking_config_override` 覆盖。

软加分策略示例（下列数值只是配置示例，不是代码默认值）：

```json
{
  "session": {
    "kv_cache_affinity": {
      "strategy": "bonus",
      "topologies": ["single", "multiple"],
      "ttl_seconds": 300,
      "age_decay": "linear",
      "bonus_by_evidence": {
        "read_hit": 0.03,
        "write_only": 0.015
      }
    }
  }
}
```

按 cache token 计算预期成本的策略：

```json
{
  "session": {
    "kv_cache_affinity": {
      "strategy": "expected_cost",
      "topologies": ["single", "multiple"],
      "ttl_seconds": 300,
      "age_decay": "linear",
      "hit_probability_by_evidence": {
        "read_hit": 0.80,
        "write_only": 0.50
      }
    }
  }
}
```

校验规则：

- `strategy` 只能是 `bonus | expected_cost`，两种结构严格互斥；
- 两种策略都必须显式提供非空、去重的 `topologies`（元素只能是 `single | multiple`）、
  正数 `ttl_seconds` 和枚举 `age_decay`；首版仅实现 `none | linear`，不能暗藏衰减常量；
- `bonus` 分支必须且只能提供两个非负 `bonus_by_evidence` 值；最大 bonus 不得超过
  同一配置中的 `session.score_delta`，避免 cache 偏好强于既有 session 路由容差；
- `expected_cost` 分支必须且只能提供两个 `[0,1]` 的命中概率；cache read/write 的
  USD 价格从实际 provider/upstream 的 canonical price catalog 读取，不在此处重复配置；
- optional 对象缺失，或当前 mode 不在 `topologies` 时，在最外层立即返回：不收集 receipt、
  不构造 guard、不注入 trace；
- packaged 配置若增加该对象，必须 bump `config_version` 和 hash；若保持缺失，则升级前后
  bytes/hash 完全不变。显式 override 即使行为等价，也会正常改变 config hash。

统一年龄衰减：

```text
d(age) = 1                                      # age_decay=none 且 age<=ttl
d(age) = max(0, 1 - age / ttl_seconds)         # age_decay=linear
d(age) = 0                                      # age>ttl_seconds
```

### 3.3 共同门禁与角色隔离

亲和条件：

1. 当前 `mode` 位于配置的 `topologies`，且同一个 `session_key`；
2. Task Analyzer 判定 `session_intent=continue` 且置信度达到现有阈值；
3. 距该角色的物理 receipt 终态时间不超过配置的 `ttl_seconds`；三个角色各自独立计算 TTL，
   不从整轮 commit 时刻重新起算；
4. cache domain 和角色完全一致；
5. 上一轮有 cache read/write 证据；
6. 当前候选仍通过全部硬过滤。

意图是硬门禁：首版及后续 token-aware 版都只在规范化后的
`intent=continue && intent_confidence >= 现有阈值` 时生效（等于阈值算通过）。
`new_task/redo/unknown`、Analyzer 失败或低置信度一律为 `0`；不得用“同 session”、
`last_route` 存在或 `sticky_applied` 代替该门禁。frozen replay 直接校验已冻结的
gate 结果。

角色匹配规则固定为：上一轮 `single` 只匹配下一轮 single，proposer 只匹配 proposer，
aggregator 只匹配 aggregator。即使 identity 相同，single 与 multiple 之间也禁止复用，
因为 direct prompt、候选 prompt、aggregator prompt 的 cache prefix 不同。

单模型当前不会读写 B5 `_router_dynamic_last_routes`，不能为了 KV cache 破坏该隔离。
新增独立、有同等 LRU 上限的 `_router_dynamic_cache_affinity` 内存态，key 至少绑定
`session_key + session_epoch + execution_topology`。容量必须读取本轮 authenticated effective
config 的 `session.route_cache_max_entries`，不能调用只读取 packaged config 的旧 helper。

`mode=single` 保持 `build_single_model_request_context(last_route={})`，不能伪造 B5 last route。
但当前 HEAD 会在 Analyzer 后的 `normalize_task_profile()` 和 ranking 的
`_apply_session_adjustment()` 两处因为 last route 为空，把 `continue` 降成 `new_task`。因此需要
两个明确、时序不同的私有输入：

1. **Analyzer 前 continuity gate**：runtime 在构造 direct request context 前，仅按
   `session_key + session_epoch + topology=single` 查询是否存在尚未过期的 single receipt，
   得到冻结的 `cache_continuity_available`。该 boolean 贯穿同一轮 analyzer primary/retry/
   fallback，显式传入 `analyze_task_with_provider/fallback_chain -> normalize_task_profile`，并在
   后续 `_apply_session_adjustment()` 复用；不得在重试间重新读取状态。
2. **Analyzer 后 candidate mapping**：`resolve_router_single_route()` 在 candidate deployment
   readiness loop 中生成按 identity 索引的 `cache_affinity_inputs`，完成 role/domain/credential/
   capability/fresh-health 匹配，再传给 `rank_single_model()` 决定每个候选的 adjustment。

continuity boolean 只允许**保留** Analyzer 已输出且达到置信阈值的 `continue`；不得把
`new_task/redo/unknown` 制造成 `continue`，也不得代替 candidate mapping。即使 continuity=true
但所有候选 guard 均不匹配，也只能保持意图，cache adjustment 仍为 0。原 `_session_score()`
始终为 0。frozen replay 冻结并校验 `cache_continuity_available` 与最终 candidate adjustment
表，不读取私有 affinity state。

显式 `turn.model` override 在 runtime 中早于 dynamic single 返回，继续保持最高优先级：
它不运行 dynamic ranking，也不读取、写入或产生 KV affinity receipt。

### 3.4 策略一：配置化软加分

软分完全来自配置：

```text
gate = 1  # 仅当 3.3 的所有门禁都通过，否则为 0
S_cache(role, model) = gate * bonus_by_evidence[evidence_kind] * d(age)
S_final = S_original + S_cache
```

不存在代码内置的 `0.03`、`0.5` 或固定 TTL。不同 evidence 的强弱直接由
`bonus_by_evidence` 配置表达。

打分位置：

- `mode=single`：direct hard filter 完成后、Top-1 排序前，把 `S_cache` 加到候选的
  direct score；raw Top-1 不可执行时仍先选下一 direct-eligible 候选，cache 不得绕过门禁；
- `mode=multiple` proposer：通过 Top-L 和 quality floor 后，把 `S_cache` 加到 greedy
  marginal；
- `mode=multiple` aggregator：hard filter 后，把 `S_cache` 加到 `Score_agg`；
- 不修改现有 role-agnostic `_session_score()`，也不把 cache 分写入 `base_clean` 或
  quality floor。

这样 cache 只是软偏好：是否换序由配置 bonus 与原候选分差共同决定。选中后若 fresh
health、credential 或 cache-domain guard 发生漂移，移除 cache adjustment 并最多重排一次；
不能带着失效 bonus dispatch。

当前 cache-domain guard 在现有 deployment/credential readiness 解析点计算，以私有 mapping
传给 ranker；不得塞入 registry snapshot 或 request context。live trace 只冻结已应用的
无秘密输入：

```text
cache_affinity_inputs = [{identity, role, strategy, score_adjustment,
                          evidence_kind, decay_factor}]
```

`decay_factor` 是本轮实际用于计算的未截断 canonical 值，必须 finite 且位于 `[0,1]`，
不使用另行定义的 age bucket；展示层可以四舍五入，但排序与 replay 只能使用 canonical 值。
replay 直接消费并校验这张
有界表，不重算私有 guard，从而能精确重放已发生的
cache-aware live 决策。配置对象缺失的 frozen replay、DRACO 和正式实验不应用 cache adjustment。

原 `N_min/N_max`、quorum、backup 机制、aggregator feasibility 与 timeout 均不改变；
亲和分进入 greedy marginal 后，实际 roster 及原有 greedy stop 得出的 proposer 数可能变化。

### 3.5 失效

以下情况令 `S_cache=0`，必要时清除亲和状态：

- `new_task`、`redo` 或 intent 置信度不足；
- 超过配置的 `ttl_seconds`；
- 自动 compaction listener 收到通知；
- session reset/delete 或 `session_epoch` 变化；
- cache domain 变化，或现有 credential-pool failure/rotation 路径撤销 session pin；
- 模型被健康、凭证、capability、context 或 canary 门禁过滤；
- 上一轮失败、fallback、usage 不完整或没有缓存证据；

失效后直接使用原路由结果，不做额外请求，也不强制回到旧模型。

### 3.6 策略二：cache token 换算为预期成本

`strategy=expected_cost` 不再额外加固定 bonus，而是用 cache token 重算候选的预期输入
费率，再替换现有 cost score 的输入部分。完整单位链如下。

#### 3.6.1 从 receipt 得到可复用 token

对当前候选角色的当轮请求：

```text
N_single = estimated_input_tokens + tool_log_tokens
N_proposer = estimated_input_tokens + tool_log_tokens
N_aggregator = estimated_input_tokens + tool_log_tokens
               + proposer_count * candidate_output_tokens

if evidence_kind == read_hit:
    K = min(N, 上一轮 cached_tokens)
elif evidence_kind == write_only:
    K = min(N, 上一轮 cache_write_tokens)

p = hit_probability_by_evidence[evidence_kind] * d(age)
```

- `N` 必须由新的、冻结的**纯输入** projection 计算；不能复用同时包含 direct/candidate/
  aggregator output budget 的 `_context_need()` 或 `_single_model_context_need()`；
- `estimated_input_tokens`、`tool_log_tokens`、`candidate_output_tokens` 与 proposer count 均取自
  本轮 authenticated ranking inputs/effective plan，不能读取事后输出长度改写决策；
- `K` 只是 `N` 中预计可命中/复用的分区，绝不是额外 token；`cached_tokens` 与
  `cache_write_tokens` 不能相加或取最大值。两者同时非零时 evidence 按 read-hit 优先，
  但 K 也只能使用 `cached_tokens`，不能把较大的 write bucket 套用 read-hit 概率；
- 上一轮无可靠 token、`N<=0`、guard 不匹配或共同门禁失败时，直接回到原成本；
- `p` 的基础值完全来自配置，年龄只按配置的 `age_decay` 衰减。

#### 3.6.2 从 token 得到单请求 USD

pricing 层先返回一个不可变、已规范化的 `CachePriceQuote`：

```text
{provider, canonical_model, cache_domain/upstream_scope, price_source,
 normal_input_per_million, cache_read_per_million, cache_write_per_million,
 cache_bucket_rates_are_total=true}
```

当前 `engine/pricing.py::PriceEntry` 的 cache read/write 字段是完整 bucket rate，不是 surcharge。
`provider/cache_affinity.py` 只消费这个已规范化 quote，不反向 import engine pricing，也不自行
解释价格目录。quote 的 provider/model/source/normal input rate 必须与当前 ranking row 中
`_model_price()` 使用的可分解 input/output price 完全一致；否则 `score_adjustment=0`。
raw scalar price、来源不明或 normal rate 不一致时均 fail closed，不能把“刷新价格源”伪装成
“cache 节省”。

OpenRouter 当前 pricing API 不能证明 strict upstream 的精确定价，所以即使 cache-domain guard
成立，没有 upstream-scoped exact quote 时 expected-cost 也退化为原成本；bonus 策略不受此
限制。未来若要支持 upstream-specific 或 surcharge catalog，需另行扩展
`engine/pricing.py`/catalog，并先归一化成上述 total-bucket quote。

quote 有效时分别计算三个互斥场景：

```text
C0    = price_input(normal=N, quote)
Chit  = price_input(normal=N-K, cache_read=K, quote)
Cmiss = price_input(normal=N-K, cache_write=K, quote)

E_input = p * Chit + (1 - p) * Cmiss       # 单位：USD / request
```

概念上，若 provider 的价格本身就是每百万 token 的完整费率，则：

```text
C0   = N * r_input / 1_000_000
Chit = ((N-K) * r_input + K * r_cache_read) / 1_000_000
```

实现只接受已声明为 total-bucket rate 的 quote。缺少 cache-read/cache-write 价格、price
source 不精确或 provider usage 语义不明确时，该候选严格退化为原成本，不估算节省。

#### 3.6.3 从 USD 换回当前排序使用的费率

当前 `_model_price()` 使用的是 `$/M tokens` 费率，而不是单请求 USD。因此必须先换回
有效输入费率，不能把 `E_input` 直接加到现有 cost term：

```text
r_input_eff = E_input / N * 1_000_000       # 单位：$/M input tokens

r_input_existing = ranking row 当前使用的 input_per_million
r_old = 当前 _model_price() 已计算出的加权费率
r_new = r_old + w_input * (r_input_eff - r_input_existing)

c_old = clamp(r_old / price_reference_usd_per_million)
c_new = clamp(r_new / price_reference_usd_per_million)

Delta_cache_cost = cost_weight * (c_old - c_new)
S_final = S_original + gate * Delta_cache_cost
```

只有 quote 与 ranking row 完全对齐时，这才与“在原 cost penalty 中只把
`input_per_million` 替换为 `r_input_eff`”完全等价。`K=0` 时直接规定
`Delta_cache_cost=0`，不得借机切换 price source。
single/aggregator 实现必须二选一：替换其 cost term，或在旧分数上加
`Delta_cache_cost`；proposer 只能在下述 greedy seam 加一次 delta。任何角色都禁止两次
应用。输出费率、质量、延迟和既有 `_session_score()` 均保持原计算。

上式中的 `S_original` 指该角色真正用于最终选择的 score：single 是 direct score，
aggregator 是 `Score_agg`，proposer 则是通过 Top-L/quality floor 后的 greedy marginal。
不能笼统修改 `_base_score_row()`，否则会把 cache adjustment 写入 `base_clean` 和质量门槛。

#### 3.6.4 数值示例

以下仅用于说明单位换算；所有值在实现中分别来自当轮 projection、部署 price catalog 和
ranking config，不是代码常量：

```text
N=100,000，K=60,000
配置命中概率=0.80，当前 age decay=0.75，所以 p=0.60
r_input=$2.00/M，r_cache_read=$0.20/M，r_cache_write=$2.50/M

C0    = $0.2000
Chit  = (40,000*2.00 + 60,000*0.20) / 1,000,000 = $0.0920
Cmiss = (40,000*2.00 + 60,000*2.50) / 1,000,000 = $0.2300
E_input = 0.60*0.0920 + 0.40*0.2300 = $0.1472/request
r_input_eff = 0.1472/100,000*1,000,000 = $1.472/M input tokens
```

若当轮 ranking config/price catalog 给出
`w_input=0.7, w_output=0.3, r_output=$8/M, price_reference=$10/M,
cost_weight=0.25`，则：

```text
r_old = 0.7*2.000 + 0.3*8.000 = 3.8000
r_new = 0.7*1.472 + 0.3*8.000 = 3.4304
Delta_cache_cost = 0.25 * (3.8000/10 - 3.4304/10) = 0.00924
```

也就是该候选最终排序分增加 `0.00924`。如果 cache write 比 normal input 更贵，或命中
概率较低，`Delta_cache_cost` 也可能接近 0 甚至为负；实现必须保留真实结果，不能强行
当作优惠。

#### 3.6.5 在 single / multiple 中如何进入排序

- `mode=single`：direct hard filter 后，为每个 direct-eligible 候选计算其 `N/K/p` 和
  `Delta_cache_cost`，再参与 Top-1 排序；只读上一轮 `single` receipt；
- `mode=multiple` proposer：先按原 base 完成 Top-L 与 quality floor，再把
  `Delta_cache_cost` **一次**加到该候选的 greedy marginal。当前 greedy 本身不使用 base/cost，
  因而 expected-cost 不改变 Top-L 候选池，也不改变 `base_clean`/quality floor；
- `mode=multiple` aggregator：在 hard filter 后替换 aggregator score 的输入成本；
- 三种角色使用各自的 token projection、receipt 与 cache-domain guard，禁止跨角色取 K。

`bonus` 与 `expected_cost` 由配置 schema 保证互斥。expected-cost 生效时不得再加
`S_cache`、不得再减一次 savings，也不得把 `K` 再加到 `N`。trace/replay 冻结以下无秘密字段：

```text
{identity, role, strategy, evidence_kind, decay_factor,
 N, K, p, price_source, C0, Chit, Cmiss,
 r_input_eff, cost_normalized_before, cost_normalized_after,
 score_adjustment}
```

这些数值按 ranking trace 的 canonical number/decimal 规则序列化；排序和 replay 使用未截断值，
展示层四舍五入不得反向参与重放。

若决定分阶段上线，建议先用 `strategy=bonus` 校验 receipt 与命中稳定性，再通过独立配置
实验启用 `strategy=expected_cost`；但两种策略的计算与互斥合同都在本设计中冻结。

## 4. 改动范围

核心实现预计改动：

- 新增 `src/opensquilla/provider/cache_affinity.py`：私有 receipt 类型、严格 cache token
  校验、evidence 分类、cache-domain guard 与 normalized `CachePriceQuote` 成本计算；它只消费
  quote，不 import `engine.pricing`，也不包含全局状态；
- `src/opensquilla/provider/deployment.py`：在 credential resolution 边界生成仅进程内的
  opaque `credential_namespace_token` 并随 private resolution 传递；不公开 secret/digest；
- `src/opensquilla/provider/ranking_router.py`：校验 optional 判别式配置；在 `rank_single_model`
  和 multiple P/A 的明确插点应用 `bonus` 或 `expected_cost`；冻结安全 replay 输入；
- `src/opensquilla/provider/ensemble.py`：multiple 每次 `chat()` 在成功关闭边界产出私有
  P/A receipt batch，并向 retry provider 透传 callback；
- `src/opensquilla/engine/runtime.py`：
  - 在 `_RouterSingleDirectProvider._chat` 的 raw Provider `Done`、close proof 和 health success
    之后产生 `single` receipt；
  - 用独立 `_router_dynamic_cache_affinity` LRU 暂存/读取三种角色状态；
  - 在最终 engine `Done` 后 one-shot commit，所有失败/取消路径 discard；
  - 在 single Analyzer 前冻结 session-level `cache_continuity_available`，把它贯穿 analyzer
    primary/retry/fallback normalization 与后续 ranking；Analyzer 后再传 candidate-specific
    `cache_affinity_inputs`，全程不伪造 B5 last route；
  - 从现有 `engine.pricing` 解析与 ranking row 对齐的 total-bucket quote；无法证明 strict
    upstream 或费率/来源不一致时不给 expected-cost adjustment；
  - 注册/注销 compaction listener，credential/session epoch 失效时清理；
- 对应 ranking、ensemble、runtime 测试。

配置支持放在现有 ranking config validator；不新增 Gateway 顶层布尔开关。operator 只有在
`ranking_config_override.session.kv_cache_affinity` 中提供完整对象时才开启。若要给 packaged
profile 默认启用，则单独修改 `router_dynamic_ranking_config.json` 并 bump 版本/hash。

monotonic 时间与 `cache_domain_guard` 不得进入 request context、selection plan 或 trace。
不要新建缓存服务、数据库、第二套 ranker或新的 single provider wrapper；复用当前
`_RouterSingleDirectProvider`。

不持久化跨进程命中率，不调整 prompt/cache marker，也不改变普通 SquillaRouter 的
`kv_cache_anti_downgrade`。本设计只覆盖 `selection_mode=router_dynamic` 下的 single/multiple。

现有 cache-break monitor 只观察 Agent 外层的 session 汇总，不能作为分角色 receipt 的权威
路由信号；仅保留其诊断用途。

核心范围只能对会触发现有 listener 的自动 compaction 立即清理；手工
`sessions.contextCompact` 当前会抑制 listener，首版由配置的 TTL 最终失效。若要“所有
compaction 立即失效”，需另外修改 `cache_break_monitor.py` 或 `rpc_sessions.py`，不属于
核心改动。

## 5. 必要测试

1. optional 对象缺失时，single/multiple 均不调用 cache helper、不收 receipt、不新增 trace；
   packaged ranking bytes/hash、P/A、single Top-1、分数、selection plan 与原结果一致；
2. 配置严格校验：两种 strategy 互斥、所有数值必填、未知键拒绝、bonus 不超过配置的
   `session.score_delta`、topologies 非空且去重、概率范围正确；实现中不存在
   bonus/TTL/概率数值 fallback；只配置 single 时 multiple 原链不读取 affinity，反之亦然；
3. bonus 策略用两组不同配置验证 `S_cache=配置值*d(age)`；single Top-1、multiple proposer
   marginal 与 aggregator score 都使用配置值，而非固定 `0.03/0.015`；
4. expected-cost 用表驱动测试逐项核对 `N/K/p/C0/Chit/Cmiss/E_input/r_input_eff` 与
   `Delta_cache_cost`；覆盖 total-bucket cache-write、clamp、`N=0`、价格缺失、raw scalar、
   strict-upstream quote 缺失以及 quote/ranking normal rate 或 source 不一致；这些 fail-closed
   情况均要求 delta=0，并证明 request USD 未与 `$/M` 直接相加；
5. `N_single/N_proposer/N_aggregator` 使用 3.6.1 的纯输入公式且不包含任何 output budget；
   single/proposer/aggregator 使用各自 projection；read/write 同时非零且 write 较大时，
   read-hit 只能使用 cached token；同一输入走 bonus/expected-cost 时只能应用一种 adjustment，
   cache token 不能重复计入 N；
6. single/proposer/aggregator 不跨角色或拓扑；cache domain/endpoint path 不同、session epoch 变化、
   upstream 非 strict 或为 `auto` 时不加分；URL 含 userinfo/query 时不建亲和，
   guard 不含 raw credential；credential resolution 只输出 `repr=False`、不可序列化的
   process-keyed opaque namespace token，secret rotation 会失配并主动 purge；同 secret 但
   `org_id` 或其他实际认证 tenant header 不同也必须产生不同 token/guard；
7. 分差大于配置 bonus 时不改变胜者，硬过滤和质量门槛始终优先；expected-cost 可以改变
   single Top-1 或 multiple roster，但不能改变硬门禁、N_min/N_max、quorum 和 backup 合同；
8. 仅 `continue` 启用；覆盖 `threshold-ε`、`threshold`、`new_task/redo/unknown`、
   Analyzer 失败、超时、自动 compaction 与 session reset；手工 compaction 在 MVP 中最晚由 TTL 失效；
9. 只有完整成功且 stream 关闭后产生的私有 receipt 能更新状态；重试/失败 usage
   行不能；覆盖 exact-int/non-bool 校验、attempt 生命周期 `usage_reported`、read-hit
   优先级、write-only、两值为 0/缺失、未知 placeholder、`physical_attempt_id` 去重、
   aggregator thinking 开/关，并证明 ensemble/session 汇总值不能产生证据；
10. single 在 `_RouterSingleDirectProvider` 的 raw Done physical boundary + health success 后才能
    stage receipt；terminal Done 后 best-effort `aclose` 失败不误丢 receipt；无 terminal/EOF 的
    required-close 路径不能产生 receipt；
    多次 Agent 工具循环的每次 `chat()` 都有独立 batch；multiple retry factory 传递 callback；
   receipt 只能经 provider 私有 collector/runtime sidecar 一次性 drain，不会出现在
   `DoneEvent`、`turn.metadata` 或序列化产物；sidecar 用 turn/decision/provider-instance
   /chat-call token 绑定，再按 physical attempt/role/slot/identity 对齐；旧 provider 延迟
   callback 不能混入，且所有异常/取消路径都清理；
11. single 不读写 B5 `_router_dynamic_last_routes`；Analyzer 前 continuity lookup 只保留
    Analyzer 自己输出的 continue，覆盖 primary/retry/fallback/frozen replay，不能制造 intent；
    Analyzer 后 candidate-specific mapping 决定每个候选的 adjustment。覆盖 continuity=true
    但 guard 全失配时 adjustment=0；显式 `turn.model` 完全旁路 affinity；
12. `actual_model` 先按冻结 alias 规则规范化，仍与 requested model 不一致时不产生
   receipt（有意的保守 false negative）；当轮 guard 只用 pre-dispatch resolved deployment
   对称计算；
13. trace 校验 `decay_factor` finite 且在 `[0,1]`，并按 identity/role 记录 canonical
    `decay_factor/score_adjustment` 及策略所需的有界
    replay 字段，不含 monotonic 时间、cache
    domain、session key、prompt 或凭证；同一 trace replay 得到同一 single Top-1 或 P/A；
14. 三种角色的 TTL 分别从各自物理 receipt 终态时刻计算，不被后续请求或整轮 commit 延长；
15. 状态读取本轮 authenticated effective config 的 `route_cache_max_entries` 上限，进程重启后
    自然清空；compaction listener
    不会因 runtime 热重建而累积。

## 6. 验收标准

- 仅当同 session 被高置信判定为 `continue` 时，single/proposer/aggregator 才能使用
  同角色的已观测缓存信号；
- bonus、TTL、衰减和命中概率全部来自配置，代码中没有 scoring magic number；
- bonus 只是软偏好；expected-cost 只替换原输入成本项；两者永远不覆盖安全与质量门禁；
- cache token 到 USD、`$/M tokens`、normalized cost 和最终 score 的单位可逐项对账；
- 缓存不可证明时行为退化为现有路由；
- 配置对象缺失时，原 single 和 multiple 执行链路均无行为、trace、hash 或成本变化。
